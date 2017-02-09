import concurrent.futures
from itertools import groupby
import logging
from operator import attrgetter
from typing import Dict, List

from etl.config import DataWarehouseSchema
from etl.extract.errors import MissingCsvFilesError, DataExtractError
import etl.monitor
from etl.relation import RelationDescription
import etl.s3
from etl.timer import Timer


class Extractor:
    """
    The 'Extractor' base class has three subclasses: static, spark, and sqoop. This base class
    defines common attributes and logic, as well as the 'extract_table' abstract method that each
    subclass must implement.
    """
    def __init__(self, schemas: Dict[str, DataWarehouseSchema],
                 descriptions: List[RelationDescription], keep_going: bool, dry_run: bool, wait: bool=True):
        self.name = None
        self.logger = logging.getLogger(self.__class__.__name__)
        self.schemas = schemas
        self.descriptions = descriptions
        self.keep_going = keep_going
        self.dry_run = dry_run
        self.wait = wait

    def extract_table(self, source: DataWarehouseSchema, description: RelationDescription):
        raise NotImplementedError(
            "Instance of {} has no proper extract_table method".format(self.__class__.__name__))

    def extract_source(self, source: DataWarehouseSchema,
                       descriptions: List[RelationDescription]) -> List[RelationDescription]:
        """
        For a given upstream source, iterate through given relations to extract the relation's data
        """
        extracted = 0
        failed = []

        with Timer() as timer:
            for description in descriptions:
                try:
                    source_monitor = {'name': source.name, 'table': description.source_table_name.table}
                    if source.is_static_source:
                        source_monitor['bucket'] = source.s3_bucket
                    else:
                        source_monitor['schema'] = description.source_table_name.schema
                    with etl.monitor.Monitor(description.identifier, 'extract', dry_run=self.dry_run,
                                             options=["with-{0.name}-extractor".format(self)],
                                             source=source_monitor,
                                             destination={'bucket_name': description.bucket_name,
                                                          'object_key': description.manifest_file_name}):
                        self.extract_table(source, description)
                except DataExtractError:
                    failed.append(description)
                    if not description.is_required:
                        self.logger.exception("Extract failed for non-required relation '%s':", description.identifier)
                    elif self.keep_going:
                        self.logger.exception("Ignoring failure of required relation and proceeding as requested:")
                    else:
                        self.logger.debug("Extract failed for required relation '%s'", description.identifier)
                        raise
                else:
                    extracted += 1
            if failed:
                self.logger.warning("Finished with %d table(s) from source '%s', %d failed (%s)",
                                    extracted, source.name, len(failed), timer)
            else:
                self.logger.info("Finished with %d table(s) from source '%s' (%s)", extracted, source.name, timer)
            return failed

    def extract_sources(self) -> None:
        """
        Iterate over sources to be extract and parallelize extraction at the source level
        """
        max_workers = len(self.schemas)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for source_name, description_group in groupby(self.descriptions, attrgetter("source_name")):
                self.logger.info("Extracting from source '%s'", source_name)
                f = executor.submit(self.extract_source, self.schemas[source_name],
                                    list(description_group))
                futures.append(f)
            if self.keep_going:
                done, not_done = concurrent.futures.wait(futures, return_when=concurrent.futures.ALL_COMPLETED)
            else:
                done, not_done = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_EXCEPTION)

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

        If the data files are in 'foo/bar/csv/part-r*', then the manifest is '/foo/bar.manifest'.

        Note that for static sources, we need to check the bucket of that source, not the
        bucket where the manifest will be written to.

        This will also test for the presence of the _SUCCESS file (added by map-reduce jobs).
        """
        self.logger.info("Preparing manifest file for data in 's3://%s/%s'", source_bucket, prefix)
        # For non-static sources, wait for data & success file to potentially finish being written
        # For static sources, we go straight to failure when the success file does not exist
        last_success = etl.s3.get_s3_object_last_modified(source_bucket, prefix + "/_SUCCESS",
                                                          wait=self.wait)
        if last_success is None and not self.dry_run:
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
            self.logger.info("Writing manifest file to 's3://%s/%s'", description.bucket_name,
                             description.manifest_file_name)
            etl.s3.upload_data_to_s3(manifest, description.bucket_name, description.manifest_file_name)
