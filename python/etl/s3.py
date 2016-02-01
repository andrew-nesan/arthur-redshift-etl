from collections import defaultdict
import logging
from fnmatch import fnmatch
import os
import os.path
import re
import tempfile
import threading

import boto3

from etl import TableName


# Split filenames into schema name, old schema, table name, and file types:
TABLE_RE = re.compile(r"""/(?P<schema>\w+)
                              /(?P<old_schema>\w+)-(?P<table>\w+)[.]
                              (?P<filetype>yml|sql|csv(.part_\d+)?.gz)$
                          """, re.VERBOSE)

_resources = threading.local()


def get_bucket(name):
    """
    Return new Bucket object for a bucket that does exist (waits until it does)
    """
    s3 = getattr(_resources, 's3', None)
    if s3 is None:
        setattr(_resources, 's3', boto3.resource('s3'))
        s3 = _resources.s3
    return s3.Bucket(name)


def upload_to_s3(filename, bucket_name, prefix, dry_run=False):
    """
    Upload local file to S3 bucket.

    Filename must be either name of file or future that will return the name of a file.
    """
    if not isinstance(filename, str):
        filename = filename.result()
    if filename is not None:
        object_key = "{}/{}".format(prefix, os.path.basename(filename))
        if dry_run:
            logging.getLogger(__name__).info("Dry-run: Skipping upload to 's3://%s/%s'", bucket_name, object_key)
        else:
            logging.getLogger(__name__).info("Uploading '%s' to 's3://%s/%s'", filename, bucket_name, object_key)
            bucket = get_bucket(bucket_name)
            bucket.upload_file(filename, object_key)


def find_files(bucket, prefix, schemas=None, pattern=None):
    """
    Find all .yml, .sql and .csv.gz files in the given bucket and folder,
    return organized by table.
    """
    if schemas is None:
        schemas = []
    logging.getLogger(__name__).info("Looking for files in 's3://%s/%s'", bucket.name, prefix)
    return find_files_from((obj.key for obj in bucket.objects.filter(Prefix=prefix)),
                           schemas=schemas, pattern=pattern)


def find_local_files(directory, schemas=None, pattern=None):
    """
    Find all SQL, table design and CSV files starting from local directory.
    """
    if schemas is None:
        schemas = []
    logging.getLogger(__name__).info("Looking for files in directory '%s'", directory)

    def list_files():
        for root, dirs, files in os.walk(os.path.normpath(directory)):
            if len(dirs) == 0:  # bottom level
                for filename in sorted(files):
                    yield os.path.join(root, filename)

    return find_files_from(list_files(), schemas=schemas, pattern=pattern)


def find_files_from(iterable, schemas=None, pattern=None):
    """
    Return a list of tuples (table name, dictionary of files) with dictionary
    containing {"Design": ..., "CSV": ..., "SQL": ...} with
    - the value of "Design" is the name of table design file
    - the value of "CSV" may be
        (1) None if no CSV file was found, or
        (2) an object key for a single CSV file, or
        (3) a prefix of a number of objects which all are compressed CSV files.
        (Both 2 and 3 may be used with Redshift's COPY command.)
    - the value of "SQL" is the name of a file with a DML which may be used within a DDL (see CTAS).

    Tables must always have a table design file. (It's not ok to have a CSV or SQL file by itself.)

    (1) If you pass in no schemas, you get no files back.
    (2) If you pass in no pattern, then you get all files back (subject to rule 1).
    What's life without whimsy?
    """
    if schemas is None:
        schemas = []
    logger = logging.getLogger(__name__)
    source_index = dict((schema, i) for i, schema in enumerate(schemas))
    found = {}
    sql_files = {}
    csv_files = defaultdict(list)
    for filename in iterable:
        match = TABLE_RE.search(filename)
        if match:
            values = match.groupdict()
            if values['schema'] in source_index:
                table_name = TableName(values['schema'], values['table'])
                sort_key = (source_index[table_name.schema], values['old_schema'], table_name.table)
                # Select based on table name from commandline args
                if not (pattern is None or fnmatch(table_name.identifier, pattern)):
                    continue
                if values['filetype'] == 'yml':
                    logger.debug("Found table design for %s in '%s'", table_name.identifier, filename)
                    found[table_name] = {"Design": filename, "CSV": None, "SQL": None, "_sort_key": sort_key}
                elif values['filetype'] == 'sql':
                    # TODO Error if more than one?
                    sql_files[table_name] = filename
                elif values['filetype'].startswith('csv'):
                    csv_files[table_name].append(filename)
    for table_name in sql_files:
        if table_name in found:
            found[table_name]["SQL"] = sql_files[table_name]
        else:
            logger.warning("Found SQL file without table design for %s", table_name.identifier)
    for table_name in csv_files:
        if table_name in found:
            found[table_name]["CSV"] = os.path.commonprefix(csv_files[table_name])
        else:
            logger.warning("Found CSV file(s) without table design for %s", table_name.identifier)
    logger.debug("Found files for %d table(s)", len(found))
    # Turn dictionary into sorted list of tuples.
    return sorted([(table_name, found[table_name]) for table_name in found],
                  key=lambda t: found[t[0]]["_sort_key"])


def get_file_content(bucket, object_key):
    """
    Download file contents from s3://bucket.name/object_key
    """
    # Download to a temp file, then read that file and remove file before returning.
    fd, name = tempfile.mkstemp()
    logging.getLogger(__name__).info("Downloading 's3://%s/%s' (using '%s')", bucket.name, object_key, name)
    bucket.download_file(object_key, name)
    os.fsync(fd)

    with open(name) as f:
        content = ''.join(f.readlines())
    os.close(fd)
    os.unlink(name)

    return content
