import concurrent.futures
from itertools import groupby
import logging
from operator import attrgetter
from typing import Dict, List, Set

from etl.config.dw import DataWarehouseSchema
from etl.errors import MissingCsvFilesError, DataExtractError, ETLRuntimeError
import etl.monitor
from etl.names import join_with_quotes
from etl.relation import RelationDescription
import etl.s3
from etl.timer import Timer


class Extractor:
    """
    The extractor base class provides the basic mechanics to
    * iterate over sources
      * iterate over tables in each source
        * call a child's class extract for a single table
    It is that method (`extract_table`) that child classes must implement.
    """
    def __init__(self, name: str, schemas: Dict[str, DataWarehouseSchema], descriptions: List[RelationDescription],
                 keep_going: bool, needs_to_wait: bool, dry_run: bool):
        self.name = name
        self.schemas = schemas
        self.descriptions = descriptions
        self.keep_going = keep_going
        # Decide whether we should wait for some application to finish dumping and writing a success file or
        # whether we can proceed immediately when testing for presence of that success file.
        self.needs_to_wait = needs_to_wait
        self.dry_run = dry_run
        self.logger = logging.getLogger(__name__)
        self.failed_sources = set()  # type: Set[str]

    def extract_table(self, source: DataWarehouseSchema, description: RelationDescription):
        raise NotImplementedError("Forgot to implement extract_table in {}".format(self.__class__.__name__))

    @staticmethod
    def source_info(source: DataWarehouseSchema, description: RelationDescription) -> Dict:
        """
        Return info for the job monitor that says from where the data is extracted.
        Defaults to the description's idea of the source but may be overridden by child classes.
        """
        return {'name': description.source_name,
                'schema': description.source_table_name.schema,
                'table': description.source_table_name.table}

    def extract_source(self, source: DataWarehouseSchema,
                       descriptions: List[RelationDescription]) -> List[RelationDescription]:
        """
        For a given upstream source, iterate through given relations to extract the relations' data.
        """
        self.logger.info("Extracting from source '%s'", source.name)
        failed = []

        with Timer() as timer:
            for description in descriptions:
                try:
                    with etl.monitor.Monitor(description.identifier, 'extract',
                                             options=["with-{0.name}-extractor".format(self)],
                                             source=self.source_info(source, description),
                                             destination={'bucket_name': description.bucket_name,
                                                          'object_key': description.manifest_file_name},
                                             dry_run=self.dry_run):
                        self.extract_table(source, description)
                except ETLRuntimeError:
                    self.failed_sources.add(source.name)
                    failed.append(description)
                    if not description.is_required:
                        self.logger.exception("Extract failed for non-required relation '%s':", description.identifier)
                    elif self.keep_going:
                        self.logger.exception("Ignoring failure of required relation '%s' and proceeding as requested:",
                                              description.identifier)
                    else:
                        self.logger.debug("Extract failed for required relation '%s'", description.identifier)
                        raise
            self.logger.info("Finished extract from source '%s': %d succeeded, %d failed (%s)",
                             source.name, len(descriptions) - len(failed), len(failed), timer)
        return failed

    def extract_sources(self) -> None:
        """
        Iterate over sources to be extracted and parallelize extraction at the source level
        """
        self.failed_sources.clear()
        # FIXME We need to evaluate whether extracting from multiple sources in parallel works with Spark!
        max_workers = len(self.schemas)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for source_name, description_group in groupby(self.descriptions, attrgetter("source_name")):
                f = executor.submit(self.extract_source, self.schemas[source_name], list(description_group))
                futures.append(f)
            if self.keep_going:
                done, not_done = concurrent.futures.wait(futures, return_when=concurrent.futures.ALL_COMPLETED)
            else:
                done, not_done = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_EXCEPTION)
        if self.failed_sources:
            self.logger.error("Failed to extract from these source(s): %s", join_with_quotes(self.failed_sources))

        # Note that iterating over result of futures may raise an exception (which surfaces exceptions from threads)
        missing_tables = []
        for future in done:
            missing_tables.extend(future.result())
        for table_name in missing_tables:
            self.logger.warning("Failed to extract: '%s'", table_name.identifier)
        if not_done:
            raise DataExtractError("Extract failed to complete for {:d} source(s)".format(len(not_done)))

    def write_manifest_file(self, description: RelationDescription, source_bucket: str, prefix: str) -> None:
        """
        Create manifest file to load all the CSV files for the given relation.
        The manifest file will be created in the folder ABOVE the CSV files.

        If the data files are in 'data/foo/bar/csv/part-r*', then the manifest is 'data/foo/bar.manifest'.

        Note that for static sources, we need to check the bucket of that source, not the
        bucket where the manifest will be written to.

        This will also test for the presence of the _SUCCESS file (added by map-reduce jobs).
        """
        self.logger.info("Preparing manifest file for data in 's3://%s/%s'", source_bucket, prefix)

        have_success = etl.s3.get_s3_object_last_modified(source_bucket, prefix + "/_SUCCESS",
                                                          wait=self.needs_to_wait and not self.dry_run)
        if have_success is None:
            if self.dry_run:
                self.logger.warning("No valid CSV files (_SUCCESS is missing)")
            else:
                raise MissingCsvFilesError("No valid CSV files (_SUCCESS is missing)")

        csv_files = sorted(key for key in etl.s3.list_objects_for_prefix(source_bucket, prefix)
                           if "part" in key and key.endswith(".gz"))
        if len(csv_files) == 0 and not self.dry_run:
            raise MissingCsvFilesError("Found no CSV files")

        remote_files = ["s3://{}/{}".format(source_bucket, filename) for filename in csv_files]
        manifest = {"entries": [{"url": name, "mandatory": True} for name in remote_files]}

        if self.dry_run:
            self.logger.info("Dry-run: Skipping writing manifest file 's3://%s/%s'",
                             description.bucket_name, description.manifest_file_name)
        else:
            self.logger.info("Writing manifest file to 's3://%s/%s'",
                             description.bucket_name, description.manifest_file_name)
            etl.s3.upload_data_to_s3(manifest, description.bucket_name, description.manifest_file_name)
