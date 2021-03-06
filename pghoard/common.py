"""
pghoard - common utility functions

Copyright (c) 2016 Ohmu Ltd
See LICENSE for details
"""
from pghoard import pgutil
from pghoard.rohmu import IO_BLOCK_SIZE
from pghoard.rohmu.compat import suppress
from pghoard.rohmu.errors import Error, InvalidConfigurationError
import datetime
import fcntl
import json
import logging
import os
import re
import tarfile
import tempfile
import time


LOG = logging.getLogger("pghoard.common")


def create_pgpass_file(connection_string_or_info):
    """Look up password from the given object which can be a dict or a
    string and write a possible password in a pgpass file;
    returns a connection_string without a password in it"""
    info = pgutil.get_connection_info(connection_string_or_info)
    if "password" not in info:
        return pgutil.create_connection_string(info)
    linekey = "{host}:{port}:{dbname}:{user}:".format(
        host=info.get("host", "localhost"),
        port=info.get("port", 5432),
        user=info.get("user", ""),
        dbname=info.get("dbname", "*"))
    pwline = "{linekey}{password}".format(linekey=linekey, password=info.pop("password"))
    pgpass_path = os.path.join(os.environ.get("HOME"), ".pgpass")
    if os.path.exists(pgpass_path):
        with open(pgpass_path, "r") as fp:
            pgpass_lines = fp.read().splitlines()
    else:
        pgpass_lines = []
    if pwline in pgpass_lines:
        LOG.debug("Not adding authentication data to: %s since it's already there", pgpass_path)
    else:
        # filter out any existing lines with our linekey and add the new line
        pgpass_lines = [line for line in pgpass_lines if not line.startswith(linekey)] + [pwline]
        content = "\n".join(pgpass_lines) + "\n"
        with open(pgpass_path, "w") as fp:
            os.fchmod(fp.fileno(), 0o600)
            fp.write(content)
        LOG.debug("Wrote %r to %r", pwline, pgpass_path)
    return pgutil.create_connection_string(info)


def connection_info_and_slot(target_node_info):
    """Process the input `target_node_info` entry which may be a libpq
    connection string or uri, or a dict containing key:value pairs of
    connection info entries or just the connection string with a replication
    slot name.  Return the connection info dict and a possible slot."""
    slot = None
    if isinstance(target_node_info, dict):
        target_node_info = target_node_info.copy()
        slot = target_node_info.pop("slot", None)
        if list(target_node_info) == ["connection_string"]:
            # if the dict only contains the `connection_string` key use it as-is
            target_node_info = target_node_info["connection_string"]
    connection_info = pgutil.get_connection_info(target_node_info)
    return connection_info, slot


def connection_string_using_pgpass(target_node_info):
    """Process the input `target_node_info` entry which may be a libpq
    connection string or uri, or a dict containing key:value pairs of
    connection info entries or just the connection string with a
    replication slot name.  Create a pgpass entry for this in case it
    contains a password and return a libpq-format connection string
    without the password in it and a possible replication slot."""
    connection_info, _ = connection_info_and_slot(target_node_info)
    return create_pgpass_file(connection_info)


def replication_connection_string_and_slot_using_pgpass(target_node_info):
    """Like `connection_string_and_slot_using_pgpass` but returns a
    connection string for a replication connection."""
    connection_info, slot = connection_info_and_slot(target_node_info)
    connection_info["dbname"] = "replication"
    connection_info["replication"] = "true"
    connection_string = create_pgpass_file(connection_info)
    return connection_string, slot


def set_stream_nonblocking(stream):
    fd = stream.fileno()
    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)


def set_subprocess_stdout_and_stderr_nonblocking(proc):
    set_stream_nonblocking(proc.stdout)
    set_stream_nonblocking(proc.stderr)


def terminate_subprocess(proc, timeout=0.1, log=None):
    if proc.poll() is None:
        if log:
            log.info("Sending SIGTERM to %r", proc)
        proc.terminate()
        timeout_time = time.time() + timeout
        while proc.poll() is None and time.time() < timeout_time:
            time.sleep(0.02)
        if proc.poll() is None:
            if log:
                log.info("Sending SIGKILL to %r", proc)
            proc.kill()
    return proc.returncode


def convert_pg_command_version_to_number(command_version_string):
    """convert a string like `psql (PostgreSQL) 9.4.4` to 90404.  also
    handle pre-release versioning where the version string is something like
    9.5alpha1 or 9.6devel"""
    match = re.search(r" \(PostgreSQL\) ([0-9]+(?:\.[0-9]+)+)", command_version_string)
    if not match:
        match = re.search(r" \(PostgreSQL\) ([0-9]+)beta([0-9])", command_version_string)
        if not match:
            raise Error("Unrecognized PostgreSQL version string {!r}".format(command_version_string))
    vernum = match.group(1) + ".0"  # padding for development versions
    parts = vernum.split(".")
    if len(parts) == 2:  # PG 10+
        return int(parts[0]) * 10000 + int(parts[1]) * 100
    return int(parts[0]) * 10000 + int(parts[1]) * 100 + int(parts[2])


def default_json_serialization(obj):
    if isinstance(obj, datetime.datetime):
        if obj.tzinfo:
            return obj.isoformat().replace("+00:00", "Z")
        # assume UTC for datetime objects without a timezone
        return obj.isoformat() + "Z"


def json_encode(obj, compact=True, binary=False):
    res = json.dumps(obj,
                     sort_keys=not compact,
                     indent=None if compact else 4,
                     separators=(",", ":") if compact else None,
                     default=default_json_serialization)
    return res.encode("utf-8") if binary else res


def write_json_file(filename, obj, *, compact=False):
    json_data = json_encode(obj, compact=compact)
    dirname, basename = os.path.dirname(filename), os.path.basename(filename)
    fd, tempname = tempfile.mkstemp(dir=dirname or ".", prefix=basename, suffix=".tmp")
    with os.fdopen(fd, "w") as fp:
        fp.write(json_data)
        if not compact:
            fp.write("\n")
    os.rename(tempname, filename)


def get_object_storage_config(config, site):
    try:
        storage_config = config["backup_sites"][site]["object_storage"]
    except KeyError:
        # fall back to `local` driver at `backup_location` if set
        if not config["backup_location"]:
            return None
        storage_config = {
            "directory": config["backup_location"],
            "storage_type": "local",
        }
    if "storage_type" not in storage_config:
        raise InvalidConfigurationError("storage_type not defined in site {!r} object_storage".format(site))
    return storage_config


def create_alert_file(config, filename):
    filepath = os.path.join(config["alert_file_dir"], filename)
    LOG.warning("Creating alert file: %r", filepath)
    with open(filepath, "w") as fp:
        fp.write("alert")


def delete_alert_file(config, filename):
    filepath = os.path.join(config["alert_file_dir"], filename)
    with suppress(FileNotFoundError):
        os.unlink(filepath)


def extract_pghoard_bb_v2_metadata(fileobj):
    # | in mode to use tarfile's internal stream buffer manager, currently required because our SnappyFile
    # interface doesn't do proper buffering for reads
    with tarfile.open(fileobj=fileobj, mode="r|", bufsize=IO_BLOCK_SIZE) as tar:
        for tarinfo in tar:
            if tarinfo.name == ".pghoard_tar_metadata.json":
                tar_meta_bytes = tar.extractfile(tarinfo).read()
                return json.loads(tar_meta_bytes.decode("utf-8"))

    raise Exception(".pghoard_tar_metadata.json not found")


def get_pg_wal_directory(config):
    if config["pg_data_directory_version"] == "10":
        return os.path.join(config["pg_data_directory"], "pg_wal")
    return os.path.join(config["pg_data_directory"], "pg_xlog")
