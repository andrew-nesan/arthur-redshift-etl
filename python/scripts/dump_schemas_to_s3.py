#!  /usr/bin/env python3

"""
Connect to source databases and download table definitions,
store them in design files locally, then upload files to S3.

While the table names remain the same, the target schema will be named after
the source name in the configuration file with the goal of assembling data from
multiple databases into one without name clashes.

If there are no previous table design files, then this code allows to
bootstrap them. If a table design file is found for a table, then that is used
instead.
"""

import argparse
import concurrent.futures
from contextlib import closing
import logging
import os
import os.path
import sys

import etl
import etl.arguments
import etl.config
import etl.dump
import etl.load
import etl.pg
import etl.s3


def normalize_and_create(directory: str, dry_run=False) -> str:
    """
    Make sure the directory exists and return normalized path to it.

    This will create all intermediate directories as needed.
    """
    name = os.path.normpath(directory)
    if not os.path.exists(name):
        if dry_run:
            logging.debug("Skipping creation of directory '%s'", name)
        else:
            logging.debug("Creating directory '%s'", name)
            os.makedirs(name)
    return name


def dump_schema_to_s3(source, table_design_files, type_maps, design_dir, bucket_name, prefix, selection, dry_run=False):
    source_name = source["name"]
    read_access = source.get("read_access")
    design_dir = normalize_and_create(os.path.join(design_dir, source_name), dry_run=dry_run)
    source_prefix = "{}/schemas/{}".format(prefix, source_name)
    found = set()
    try:
        logging.info("Connecting to source database '%s'", source_name)
        with closing(etl.pg.connection(etl.env_value(read_access), autocommit=True, readonly=True)) as conn:
            tables = etl.dump.fetch_tables(conn, source["include_tables"], source.get("exclude_tables", []), selection)
            columns_by_table = etl.dump.fetch_columns(conn, tables)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                for source_table_name in sorted(columns_by_table):
                    table_name = etl.TableName(source_name, source_table_name.table)
                    found.add(source_table_name)
                    columns = etl.dump.map_types_in_ddl(source_table_name,
                                                        columns_by_table[source_table_name],
                                                        type_maps["as_is_att_type"],
                                                        type_maps["cast_needed_att_type"])
                    table_design = etl.dump.create_table_design(source_name, source_table_name, table_name, columns)
                    if table_name in table_design_files:
                        # Replace bootstrapped table design with one from file but check whether set of columns changed.
                        design_file = table_design_files[table_name]
                        with open(design_file) as f:
                            existing_table_design = etl.load.load_table_design(f, table_name)
                        etl.load.compare_columns(table_design, existing_table_design)
                    else:
                        design_file = executor.submit(etl.dump.save_table_design,
                                                      table_design, source_table_name, design_dir, dry_run=dry_run)
                    executor.submit(etl.s3.upload_to_s3, design_file, bucket_name, source_prefix, dry_run=dry_run)
    except Exception:
        logging.exception("Error while processing source '%s'", source_name)
        raise
    not_found = found.difference(set(table_design_files))
    if len(not_found):
        logging.warning("New tables which had no design: %s", sorted(table.identifier for table in not_found))
    too_many = set(table_design_files).difference(found)
    if len(too_many):
        logging.warning("Table design files without tables: %s", sorted(table.identifier for table in too_many))
    logging.info("Done with %d table(s) from source '%s'", len(found), source_name)


def dump_schemas_to_s3(args, settings):
    bucket_name = settings("s3", "bucket_name")
    selection = etl.TableNamePatterns.from_list(args.table)
    schemas = [source["name"] for source in settings("sources") if selection.match_schema(source["name"])]
    local_files = etl.s3.find_local_files([args.table_design_dir], schemas, selection)

    # Check that all env vars are set--it's annoying to have this fail for the last source without upfront warning.
    for source in settings("sources"):
        if source["name"] in schemas and "read_access" in source:
            if source["read_access"] not in os.environ:
                raise KeyError("Environment variable not set: %s" % source["read_access"])

    with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
        for source in settings("sources"):
            source_name = source["name"]
            if source_name not in schemas:
                continue
            if "read_access" not in source:
                logging.info("Skipping empty source '%s' (no environment variable to use for connection)", source_name)
                continue
            table_design_files = {assoc_files.source_table_name: assoc_files.design_file
                                  for assoc_files in local_files[source_name]}
            logging.debug("Submitting job to download from '%s'", source_name)
            pool.submit(dump_schema_to_s3, source, table_design_files, settings("type_maps"),
                        args.table_design_dir, bucket_name, args.prefix, selection,
                        dry_run=args.dry_run)


def check_positive_int(s):
    """
    Helper method for argument parser to make sure optional arg with value 's'
    is a positive integer (meaning, s > 0)
    """
    try:
        i = int(s)
        if i <= 0:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("%s is not a positive int" % s)
    return i


def build_argument_parser():
    parser = etl.arguments.argument_parser(["config", "prefix", "prefix_env", "table-design-dir",
                                            "dry-run", "table"], description=__doc__)
    parser.add_argument("-j", "--jobs", help="Number of parallel connections (default: %(default)s)",
                        type=check_positive_int, default=1)
    return parser


if __name__ == "__main__":
    main_args = build_argument_parser().parse_args()
    etl.config.configure_logging(main_args.log_level)
    main_settings = etl.config.load_settings(main_args.config)
    try:
        with etl.measure_elapsed_time(), etl.pg.log_error():
            dump_schemas_to_s3(main_args, main_settings)
    except:
        # Exception has already been logged, so just bail out gracefully.
        sys.exit(1)