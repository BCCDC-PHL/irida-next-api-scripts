#!/usr/bin/env python

import argparse
import base64
import csv
import json
import logging
import os
import re
import sys

from collections.abc import Generator
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any

from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
from gql.transport.aiohttp import AIOHTTPTransport
from gql.transport.exceptions import TransportQueryError

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

log = logging.getLogger()

###
# Classes
###

class EnhancedJSONEncoder(json.JSONEncoder):
    """
    Helper for serializing Dataclasses to JSON (mostly for troubleshooting/debugging).
    """
    def default(self, o):
        if isinstance(o, Path):
            return str(o)
        return super().default(o)

@dataclass
class Config:
    """
    Core app config values. Can be supplied from
    config file or cmdline args.
    """
    api_base_url: str
    email: str
    token: str
    project_puid: str|None = None
    sample_name: str|None = None
    sample_puid: str|None = None
    input: Path|None = None
    exclude_by_regex: list[str] = field(default_factory=list)
    select_by_regex: list[str] = field(default_factory=list)


@dataclass
class Input:
    """
    One record from the input, representing a
    sample for which attachments should be downloaded.
    """
    sample_name: str|None = None
    sample_puid: str|None = None
    project_puid: str|None = None


def parse_input_file(input_path: Path) -> list[dict]:
    """
    Parse and input (samplesheet) file.

    Expected fields include:
    project_puid
    sample_puid
    sample_name
    """
    parsed_inputs = []

    with open(input_path, 'r') as f:
        reader = csv.DictReader(f, delimiter=',')
        for row in reader:
            parsed_input = {}
            for k, v in row.items():
                k = k.replace(' ', '_').lower()
                parsed_input[k] = v

            parsed_inputs.append(parsed_input)
            
    return parsed_inputs


def create_basic_token(email: str, token: str):
    """
    Given an IRIDA personal access token, prepare it for use in HTTP header.
    """
    message = f"{email}:{token}"
    message_bytes = message.encode('utf-8')
    base64_bytes = base64.b64encode(message_bytes)
    base64_message = base64_bytes.decode('utf-8')

    return base64_message


def run_query(client: Client, query_str: str, variable_values: dict = {}, max_retries = 0) -> dict|None:
    """
    Run a GraphQL query given a client and query. Optionally supply a variables dict.
    """
    query = gql(query_str)
    query.variable_values = variable_values
    result = None
    num_retries = 0
    while num_retries <= max_retries:
        try:
            result = client.execute(query)
        except TransportQueryError as e:
            if hasattr(e, 'errors'):
                errors = e.errors
                if errors:
                    for error in errors:
                        error_message = error.get('message', '')
                        log.error(f"TransportQueryError: {error_message}")

        num_retries += 1

    return result


def get_project_puids_for_group(client: Client, group_puid: str) -> list[str]:
    """
    """
    query_variables = {
        'groupId': group_puid,
    }

    query = """
query GetProjects($groupId: ID!){
  group(puid: $groupId) {
    projects {
      nodes {
        puid
      }
    }
  }
}
"""
    response = run_query(client, query, query_variables)

    project_puids = []
    project_nodes = response.get('group', {}).get('projects', {}).get('nodes')
    if not project_nodes:
        log.error(f"Failed to get projects for group: {group_puid}")
        return project_puids
    for p in project_nodes:
        project_puids.append(p['puid'])

    return project_puids

def get_sample_puids_for_project(client: Client, project_puid: str) -> list[str]:
    """
    """

    query_variables = {
        'projectId': project_puid,
    }

    sample_count_query = """
query GetSampleCount($projectId: ID!){
  project(puid: $projectId) {
    samples(first: 1){
      totalCount
      pageInfo {
        startCursor
        endCursor
        hasNextPage
      }
    }
  }
}
"""
    sample_puids = []
    response = run_query(client, sample_count_query, query_variables)
    num_samples = response.get('project', {}).get('samples', {}).get('totalCount')
    start_cursor = response.get('project', {}).get('samples', {}).get('pageInfo', {}).get('startCursor')
    if num_samples:
        log.info(f"Found {num_samples} samples on project: {project_puid}")
    

    return sample_puids

def get_sample_puid(client: Client, project_puid: str, sample_name: str) -> str|None:
    """
    Given an IRIDA Project PUID and a sample name, return the sample PUID, or None if not found.
    """
    sample_puid = None

    sample_query_variables = {
        'projectId': project_puid,
        'sampleName': sample_name,
    }

    sample_query =  """
query GetSample($projectId: ID!, $sampleName: String){
  project(puid: $projectId){
    samples(filter: {name_or_puid_cont: $sampleName}){
      nodes{
        name
        puid
      }
    }
  }
}
"""

    sample_response = run_query(client, sample_query, sample_query_variables)
    if not sample_response:
        log.error(f"{sample_name}: Sample not found in project {project_puid}")
        return None
    samples = sample_response.get('project', {}).get('samples', {})
    if not samples:
        log.error(f"{sample_name}: Sample not found in project {project_puid}")
        return None

    samples_nodes = samples.get('nodes', [])

    if len(samples_nodes) == 1:
        sample_puid = samples_nodes[0]['puid']
        log.info(f"Found sample {sample_name} with puid {sample_puid} on project {project_puid}")
    else:
        log.error(f"{sample_name}: Failed to get puid for {sample_name} on project {project_puid}")
        return None

    return sample_puid


def get_sample_metadata(client: Client, sample_puid: str) -> dict|None:
    """
    Given an IRIDA Sample PUID, return a generator for attachments
    """
    cursor = None
    query_variable_values = {
        'sampleId': sample_puid,
        'numAttachments': 1,
        'afterCursor': cursor,
    }

    query = """
query GetSampleAttachments($sampleId: ID!){
  sample(puid: $sampleId){
    metadata
  }   
}
"""
    sample_metadata_response = run_query(client, query, query_variable_values)
    if not sample_metadata_response:
        log.error(f"{sample_puid}: Failed to fetch response for sample")
        return None
    metadata = sample_metadata_response.get('data', {}).get('sample', {}).get('metadata')
    if not metadata:
        log.error(f"{sample_puid}: No metadata found for sample.")
        return None

    return metadata


def parse_config(config_path: Path) -> dict:
    """
    Parse json config file, return config as dict.
    """
    config = {}
    with open(config_path) as f:
        config = json.load(f)

    return config


def merge_config_with_args(args: argparse.Namespace, config: dict[str, Any]) -> Config:
    """
    Merge values from config file with those from cmd args. Cmd args take prescedence when supplied.
    Returns a Config dataclass.
    """
    merged_config = config.copy()
    if args.api_base_url:
        merged_config['api_base_url'] = args.api_base_url
    if args.email:
        merged_config['email'] = args.email
    if args.token:
        merged_config['token'] = args.token
    if args.input:
        merged_config['input'] = args.input
    if args.group_puid:
        merged_config['group_puid'] = args.group_puid
    if args.project_puid:
        merged_config['project_puid'] = args.project_puid
    if args.sample_name:
        merged_config['sample_name'] = args.sample_name
    if args.sample_puid:
        merged_config['sample_puid'] = args.sample_puid
    if args.exclude_by_regex:
        merged_config['exclude_by_regex'] = args.exclude_by_regex
    if args.select_by_regex:
        merged_config['select_by_regex'] = args.select_by_regex
    if args.output:
        merged_config['output'] = args.outdir

    valid_config_fields = {f.name for f in fields(Config)}
    filtered_merged_config_dict = {k: v for k, v in merged_config.items() if k in valid_config_fields}

    parsed_config = Config(**filtered_merged_config_dict)

    return parsed_config


def merge_inputs_with_config(inputs_from_file: list, config: Config) -> list[Input]:
    """
    Merge input file with config info.
    """
    merged_inputs_list = []
    
    if not inputs_from_file:
        inputs_from_file = []
        
    provided_inputs = inputs_from_file
    if config.sample_name or config.sample_puid:
        config_input = {
            'sample_name': config.sample_name,
            'sample_puid': config.sample_puid,
            'project_puid': config.project_puid,
        }
        provided_inputs += [config_input]

    for i in provided_inputs:
        if not i.get('project_puid'):
            i['project_puid'] = config.project_puid
        
        if not i.get('sample_puid'):
            sample_name = i.get('sample_name')
            project_puid = i.get('project_puid')
            if not all([sample_name, project_puid]):
                log.error(f"Both sample_name and project_puid are required if sample_puid is not supplied. sample_name: {sample_name}, project_puid: {project_puid}")
                continue

        parsed_input = Input(**i)
        merged_inputs_list.append(parsed_input)
            
    return merged_inputs_list


def is_selected_by_regex(filename: str, selected_regexes: list[str] = ['*']):
    """
    Check if an attachment is selected, based on its filename
    and the supplied regexes.
    """
    for regex in selected_regexes:
        if re.match(regex, filename):
            log.debug(f"Selecting file {filename} based on regex '{regex}'")
            return regex
    return False


def is_excluded_by_regex(filename: str, excluded_regexes: list[str] = []):
    """
    Check if an attachment is excluded, based on its filename
    and the supplied regexes.
    """
    for regex in excluded_regexes:
        if re.match(regex, filename):
            log.debug(f"Excluding file {filename} based on regex '{regex}'")
            return regex
    return False


def main(args):
    """
    Download sample attachments via the IRIDA Next GraphQL API.
    """

    config_dict = {}
    if args.config:
        if not os.path.exists(args.config):
            log.error(f"Config file not found at: {args.config}")
            exit(1)
        try:
            config_dict = parse_config(args.config)
        except Exception as e:
            log.error(f"Failed to load config at: {args.config}")
            exit(1)

    config = merge_config_with_args(args, config_dict)
    # print(json.dumps(asdict(config), indent=2, cls=EnhancedJSONEncoder))
    # exit()

    log.info("Configuration complete.")

    inputs = []
    if config.input:
        inputs = parse_input_file(config.input)

    inputs = merge_inputs_with_config(inputs, config)
    
    basic_token = create_basic_token(config.email, config.token)

    transport = RequestsHTTPTransport(
        url=config.api_base_url,
        headers={"Authorization": f"Basic {basic_token}"},
        use_json=True,
    )
    # TODO: try async transport
    # transport = AIOHTTPTransport(url=config.api_base_url)
    client = Client(transport=transport, fetch_schema_from_transport=True)
    log.info('Connection established.')
    project_puids = []
    if args.group_puid:
        group_project_puids = get_project_puids_for_group(client, args.group_puid)
        for project_puid in group_project_puids:
            sample_puids = get_sample_puids_for_project(client, project_puid)

        
    

    

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', type=Path, default=Path.home() / '.config' / 'irida-next-client' / 'config.json', help="""
Optional JSON config file. Can be used to provide "api_base_url", "email", "token", "outdir", using those keys. (default: "~/.config/irida-next-client/config.json")
""")
    parser.add_argument('--api-base-url', default='https://irida-next.nml-lnm.phac-aspc.gc.ca/api/graphql')
    parser.add_argument('-e', '--email', help="email address for user whose access token is used")
    parser.add_argument('-t', '--token', help="IRIDA Next access token (see: https://phac-nml.github.io/irida-next/docs/extend/graphql#authentication-with-personal-access-tokens)")
    parser.add_argument('-i', '--input', help="""
CSV input file with headers "project_puid", "sample_puid", "sample_name".
If "sample_puid" is not supplied, then "project_puid" and "sample_name" are both required.
If "project_puid" is not supplied in input file, then it is required via "--project-puid" flag.
""")
    parser.add_argument('--group-puid', help="IRIDA Group PUID.")
    parser.add_argument('--project-puid', help="IRIDA Project PUID. Required if not supplied via input csv file.")
    parser.add_argument('--sample-puid', help="IRIDA Sample PUID.")
    parser.add_argument('--sample-name', help="IRIDA Sample Name. Project PUID required if Sample PUID is not supplied")
    parser.add_argument('--select-by-regex', nargs='*', help="Select attachments by regex match against filename (selection supersedes exclusion).")
    parser.add_argument('--exclude-by-regex', nargs= '*', help="Exclude attachments by regex match against filename (selection supersedes exclusion).")
    parser.add_argument('-o', '--output', type=Path, help="File to write metadata to, or stdout if not supplied.")
    args = parser.parse_args()
    main(args)
