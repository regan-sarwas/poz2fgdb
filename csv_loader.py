# -*- coding: utf-8 -*-
"""
Module to load Park Observer CSV files into an esri file geodatabase.

Written for Python 2.7; works with Python 3.x.
Requires the Esri ArcGIS arcpy module.
Requires python-dateutil module which is included with ArcGIS 10.x and Pro 2.x.

NOTE: esri does not provide python wrappers for the objects in arcpy.da, so
they will generate errors with linters and IDE's cannot do code completion.
See: https://gis.stackexchange.com/a/120517
"""

from __future__ import absolute_import, division, print_function, unicode_literals

import csv
from io import open
import glob
import os
import sys

import arcpy
import dateutil.parser

import database_creator

# MACROS: Key indexes for GPS data in CSV data (T=Timestamp, X=Longitude, Y=Latitude)
T, X, Y = 0, 1, 2


def open_csv_read(filename):
    """Open a file for CSV reading that is compatible with unicode and Python 2/3"""
    if sys.version_info[0] < 3:
        return open(filename, "rb")
    return open(filename, "r", encoding="utf8", newline="")


def process_csv_folder(csv_path, protocol, database_path):
    """Build a set of feature classes for a folder of CSV files.

    Takes a file path to a folder of CSV files (string), a PO protocol object,
    and a file path to an existing fgdb (string).

    There is no return value.
    """
    version = protocol["meta-version"]
    if version <= 2:
        process_csv_folder_v1(csv_path, protocol, database_path)
    else:
        print("Unable to process protocol specification version {0}.".format(version))


def process_csv_folder_v1(csv_path, protocol, database_path):
    """Build a set of feature classes for a folder of CSV files (in version 1 format)."""
    csv_files = glob.glob(csv_path + r"\*.csv")
    csv_filenames = [
        os.path.splitext(os.path.basename(csv_file))[0] for csv_file in csv_files
    ]
    gps_points_csv_name = protocol["csv"]["gps_points"]["name"]
    track_logs_csv_name = protocol["csv"]["track_logs"]["name"]
    gps_points_list = None
    track_log_oids = None
    # An edit session is needed to add items in a relationship,
    # and to have multiple open insert cursors
    # The with statement handles saving and aborting the edit session
    with arcpy.da.Editor(database_path):
        if (
            track_logs_csv_name in csv_filenames
            and gps_points_csv_name in csv_filenames
        ):
            track_log_oids = process_tracklog_path_v1(
                csv_path,
                gps_points_csv_name,
                track_logs_csv_name,
                protocol,
                database_path,
            )
            csv_filenames.remove(track_logs_csv_name)
        if gps_points_csv_name in csv_filenames:
            gps_points_list = process_gpspoints_path_v1(
                csv_path, gps_points_csv_name, protocol, database_path, track_log_oids
            )
            csv_filenames.remove(gps_points_csv_name)
        for feature_name in csv_filenames:
            process_feature_path_v1(
                csv_path, feature_name, gps_points_list, protocol, database_path
            )


def process_tracklog_path_v1(
    csv_path, gps_point_filename, track_log_filename, protocol, database_path
):
    """Process the CSV file of track log and return the object IDs of the new track logs."""
    point_path = os.path.join(csv_path, gps_point_filename + ".csv")
    track_path = os.path.join(csv_path, track_log_filename + ".csv")
    gps_points_header = ",".join(protocol["csv"]["gps_points"]["field_names"])
    track_log_header = ",".join(protocol["csv"]["track_logs"]["field_names"])
    with open(point_path, "r", encoding="utf-8") as point_f, open_csv_read(
        track_path
    ) as track_f:
        point_header = point_f.readline().rstrip()
        track_header = track_f.readline().rstrip()
        if point_header == gps_points_header and track_header.endswith(
            track_log_header
        ):
            return process_tracklog_file_v1(point_f, track_f, protocol, database_path)
        return {}


def process_tracklog_file_v1(point_file, track_file, protocol, database_path):
    """Build a track log feature class and return the object IDs of the new track logs."""
    # pylint: disable=too-many-locals
    print("building track logs")
    track_log_oids = {}
    mission_field_names, mission_field_types = extract_mission_attributes_from_protocol(
        protocol
    )
    mission_fields_count = len(mission_field_names)
    columns = (
        ["SHAPE@"] + mission_field_names + protocol["csv"]["track_logs"]["field_names"]
    )
    types = protocol["csv"]["track_logs"]["field_types"]
    table_name = protocol["csv"]["track_logs"]["name"]
    table = os.path.join(database_path, table_name)
    s_key = protocol["csv"]["track_logs"]["start_key_indexes"]
    e_key = protocol["csv"]["track_logs"]["end_key_indexes"]
    gps_keys = protocol["csv"]["gps_points"]["key_indexes"]
    last_point = None
    # Need a schema lock to drop/create the index
    #    arcpy.RemoveSpatialIndex_management(table)
    with arcpy.da.InsertCursor(table, columns) as cursor:
        for line in csv.reader(track_file):
            # each line in the CSV is a list of items; the type of item is
            #  str (unicode) in Python 3
            #  utf8 encode byte string in Python 2, converted to unicode strings
            if sys.version_info[0] < 3:
                items = [item.decode("utf-8") for item in line]
            else:
                items = line
            protocol_items = items[:mission_fields_count]
            other_items = items[mission_fields_count:]
            start_time, end_time = other_items[s_key[T]], other_items[e_key[T]]
            track, last_point = build_track_geometry(
                point_file, last_point, start_time, end_time, gps_keys
            )
            row = (
                [track]
                + [
                    cast(item, mission_field_types[i])
                    for i, item in enumerate(protocol_items)
                ]
                + [cast(item, types[i]) for i, item in enumerate(other_items)]
            )
            track_log_oids[start_time] = cursor.insertRow(row)
    #    arcpy.AddSpatialIndex_management(table)
    return track_log_oids


def process_gpspoints_path_v1(
    csv_path, gps_point_filename, protocol, database_path, track_log_oids=None
):
    """Add a CSV file of GPS points to the database."""
    path = os.path.join(csv_path, gps_point_filename + ".csv")
    gps_points_header = ",".join(protocol["csv"]["gps_points"]["field_names"])
    with open(path, "r", encoding="utf-8") as handle:
        header = handle.readline().rstrip()
        if header == gps_points_header:
            return process_gpspoints_file_v1(
                handle, track_log_oids, protocol, database_path
            )
        return {}


def process_gpspoints_file_v1(
    file_without_header, tracklog_oids, protocol, database_path
):
    """Build a GPS points feature class and return the new features."""
    # pylint: disable=too-many-locals
    print("building gps points")
    results = {}
    columns = ["SHAPE@XY"] + protocol["csv"]["gps_points"]["field_names"]
    if tracklog_oids:
        columns.append("TrackLog_ID")
    table_name = protocol["csv"]["gps_points"]["name"]
    table = os.path.join(database_path, table_name)
    types = protocol["csv"]["gps_points"]["field_types"]
    key = protocol["csv"]["gps_points"]["key_indexes"]
    current_track_oid = None
    # Need a schema lock to drop/create the index
    #    arcpy.RemoveSpatialIndex_management(table)
    with arcpy.da.InsertCursor(table, columns) as cursor:
        for line in file_without_header:
            items = line.split(",")
            shape = (float(items[key[X]]), float(items[key[Y]]))
            row = [shape] + [cast(item, types[i]) for i, item in enumerate(items)]
            if tracklog_oids:
                try:
                    current_track_oid = tracklog_oids[items[key[T]]]
                except KeyError:
                    pass
                row.append(current_track_oid)
            results[items[key[T]]] = cursor.insertRow(row)
    #    arcpy.AddSpatialIndex_management(table)
    return results


def process_feature_path_v1(
    csv_path, feature_name, gps_points_list, protocol, database_path
):
    """Add a feature's CSV file to the database."""
    feature_path = os.path.join(csv_path, feature_name + ".csv")
    feature_header = protocol["csv"]["features"]["header"]
    with open_csv_read(feature_path) as feature_f:
        file_header = feature_f.readline().rstrip()
        if file_header.endswith(feature_header):
            process_feature_file_v1(
                feature_f, protocol, gps_points_list, feature_name, database_path
            )


def process_feature_file_v1(
    feature_f, protocol, gps_points_list, feature_name, database_path
):
    """Build a feature class in the database for a named feature."""
    # pylint: disable=too-many-locals,broad-except
    print("building {0} features and observations".format(feature_name))

    feature_field_names, feature_field_types = extract_feature_attributes_from_protocol(
        protocol, feature_name
    )
    feature_fields_count = len(feature_field_names)

    feature_table_name = arcpy.ValidateTableName(feature_name, database_path)
    feature_table = os.path.join(database_path, feature_table_name)
    feature_columns = (
        ["SHAPE@XY"]
        + feature_field_names
        + protocol["csv"]["features"]["feature_field_names"]
        + ["GpsPoint_ID", "Observation_ID"]
    )
    feature_types = protocol["csv"]["features"]["feature_field_types"]
    feature_field_map = protocol["csv"]["features"]["feature_field_map"]
    f_key = protocol["csv"]["features"]["feature_key_indexes"]

    observation_table_name = protocol["csv"]["features"]["obs_name"]
    observation_table = os.path.join(database_path, observation_table_name)
    observation_columns = (
        ["SHAPE@XY"] + protocol["csv"]["features"]["obs_field_names"] + ["GpsPoint_ID"]
    )
    observation_types = protocol["csv"]["features"]["obs_field_types"]
    observation_field_map = protocol["csv"]["features"]["obs_field_map"]
    o_key = protocol["csv"]["features"]["obs_key_indexes"]

    # Need a schema lock to drop/create the index
    #    arcpy.RemoveSpatialIndex_management(feature_table)
    #    arcpy.RemoveSpatialIndex_management(observation_table)
    with arcpy.da.InsertCursor(
        feature_table, feature_columns
    ) as feature_cursor, arcpy.da.InsertCursor(
        observation_table, observation_columns
    ) as observation_cursor:
        for line in csv.reader(feature_f):
            # Skip empty lines (happens in some buggy versions)
            if not line:
                break
            # each line in the CSV is a list of items; the type of item is
            #  str (unicode) in Python 3
            #  utf8 encode byte string in Python 2, converted to unicode strings
            if sys.version_info[0] < 3:
                items = [item.decode("utf-8") for item in line]
            else:
                items = line
            protocol_items = items[:feature_fields_count]
            other_items = items[feature_fields_count:]
            feature_items = filter_items_by_index(other_items, feature_field_map)
            observe_items = filter_items_by_index(other_items, observation_field_map)

            feature_timestamp = feature_items[f_key[T]]
            feature_shape = (
                float(feature_items[f_key[X]]),
                float(feature_items[f_key[Y]]),
            )
            observation_timestamp = observe_items[o_key[T]]
            observation_shape = (
                float(observe_items[o_key[X]]),
                float(observe_items[o_key[Y]]),
            )
            try:
                feature_gps_oid = gps_points_list[feature_timestamp]
            except KeyError:
                feature_gps_oid = None
            try:
                observation_gps_oid = gps_points_list[observation_timestamp]
            except KeyError:
                observation_gps_oid = None
            try:
                feature = (
                    [feature_shape]
                    + [
                        cast(item, feature_field_types[i])
                        for i, item in enumerate(protocol_items)
                    ]
                    + [
                        cast(item, feature_types[i])
                        for i, item in enumerate(feature_items)
                    ]
                    + [feature_gps_oid]
                )
                observation = (
                    [observation_shape]
                    + [
                        cast(item, observation_types[i])
                        for i, item in enumerate(observe_items)
                    ]
                    + [observation_gps_oid]
                )
            except Exception:
                arcpy.AddWarning(
                    "Skipping Bad Record.  Table: {0}; Record: {1}".format(
                        feature_table, line
                    )
                )
                continue
            observation_oid = observation_cursor.insertRow(observation)
            feature.append(observation_oid)
            feature_cursor.insertRow(feature)


#    arcpy.AddSpatialIndex_management(feature_table)
#    arcpy.AddSpatialIndex_management(observation_table)


# Support functions


def cast(string, esri_type):
    """Convert a string to an esri data type and return it or None."""
    esri_type = esri_type.upper()
    if esri_type in ("DOUBLE", "FLOAT"):
        return maybe_float(string)
    if esri_type in ("SHORT", "LONG"):
        return maybe_int(string)
    if esri_type == "DATE":
        # In Python3, the parser complains (issues a one time warning that kills
        # the app), that the AKST/AKDT timezone suffix could be ambiguous (it isn't
        # but oh well), and that in future version it might be an exception.
        # Since ArcGIS does not understand time zones, and we have two date fields
        # one for UTC and one for local, we can ignore the timezone info while
        # parsing.
        return dateutil.parser.parse(string, ignoretz=True)
    if esri_type in ("TEXT", "BLOB"):
        return string
    return None


def build_track_geometry(point_file, prior_last_point, start_time, end_time, keys):
    """Build and return a polyline, and last point for a track log."""
    if prior_last_point:
        path = [prior_last_point]
    else:
        path = []
    point = None
    for line in point_file:
        items = line.split(",")
        timestamp = items[keys[T]]
        if timestamp <= start_time:
            path = []
        if timestamp < start_time:
            continue
        point = [float(items[keys[X]]), float(items[keys[Y]])]
        path.append(point)
        if timestamp == end_time:
            break
    esri_json = {"paths": [path], "spatialReference": {"wkid": 4326}}
    polyline = arcpy.AsShape(esri_json, True)
    return polyline, point


def extract_mission_attributes_from_protocol(protocol):
    """Extract and return the field names/types from a protocol file mission."""
    field_names = []
    field_types = []
    # mission is optional in Park Observer 2.0
    if "mission" in protocol:
        attributes = database_creator.get_attributes(protocol["mission"])
        for attribute in attributes:
            field_names.append(attribute["name"])
            field_types.append(attribute["type"])
    return field_names, field_types


def extract_feature_attributes_from_protocol(protocol, feature_name):
    """Extract and return the field names/types from a protocol file feature."""
    field_names = []
    field_types = []
    attributes = None
    for feature in protocol["features"]:
        if feature["name"] == feature_name:
            attributes = database_creator.get_attributes(feature)
    for attribute in attributes:
        field_names.append(attribute["name"])
        field_types.append(attribute["type"])
    return field_names, field_types


def filter_items_by_index(items, indexes):
    """
    Gets a re-ordered subset of items
    :param items: A list of values
    :param indexes: a list of index in items
    :return: the subset of values in items at just indexes
    """
    results = []
    for i in indexes:
        results.append(items[i])
    return results


def maybe_float(string):
    """Convert string to a float and return the float or None."""
    try:
        return float(string)
    except ValueError:
        return None


def maybe_int(string):
    """Convert string to an integer and return the integer or None."""
    try:
        return int(string)
    except ValueError:
        return None


def test():
    """Create a database and load a folder of CSV data."""
    protocol_path = r"\\akrgis.nps.gov\inetApps\observer\protocols\sample.obsprot"
    fgdb_folder = r"C:\tmp\observer"
    csv_folder = r"C:\tmp\observer\test1"
    database, protocol_json = database_creator.database_for_protocol_file(
        protocol_path, fgdb_folder
    )
    process_csv_folder(csv_folder, protocol_json, database)


if __name__ == "__main__":
    test()
