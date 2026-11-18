#!/usr/bin/env python

import argparse
import base64
import csv
import json
import logging
import os
import re
import signal
import sys

from collections.abc import Generator
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any

from requests.cookies import extract_cookies_to_jar

from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
from gql.transport.aiohttp import AIOHTTPTransport
from gql.transport.exceptions import TransportQueryError

import requests

log = logging.getLogger()

def configure_logging(log_level: str="info"):
    logging.basicConfig(
        level=log_level.upper(),
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

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
    group_puid: str|None = None
    project_puid: str|None = None
    input: Path|None = None
    exclude_by_regex: list[str] = field(default_factory=list)
    select_by_regex: list[str] = field(default_factory=list)


@dataclass
class Input:
    """
    One record from the input, representing a
    sample for which metadata should be downloaded.
    """
    sample_name: str|None = None
    sample_puid: str|None = None


def parse_input_file(input_path: Path) -> list[dict]:
    """
    Parse and input (samplesheet) file.

    Expected fields include:
    sample_puid OR sample_name
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


def run_query(client: Client, query_str: str, variable_values: dict = {}, max_retries = 0) -> dict:
    """
    Run a GraphQL query given a client and query. Optionally supply a variables dict.
    """
    query = gql(query_str)
    query.variable_values = variable_values
    result = {}
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


def is_selected_by_regex(sample_name: str, selected_regexes: list[str] = ['*']):
    """
    Check if a sample is selected, based on its sample_name
    and the supplied regexes.
    """
    for regex in selected_regexes:
        if re.match(regex, sample_name):
            log.debug(f"Selecting file {sample_name} based on regex '{regex}'")
            return regex
    return False

def is_excluded_by_regex(sample_name: str, excluded_regexes: list[str] = []):
    """
    Check if a sample is excluded, based on its sample_name
    and the supplied regexes.
    """
    for regex in excluded_regexes:
        if re.match(regex, sample_name):
            log.debug(f"Excluding file {sample_name} based on regex '{regex}'")
            return regex
    return False

def get_sample_count_for_project(client: Client, project_puid: str) -> int:
    """
    """
    sample_count_query_variables = {
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
    num_samples_response = run_query(client, sample_count_query, sample_count_query_variables)
    num_samples = num_samples_response.get('project', {}).get('samples', {}).get('totalCount')

    return num_samples

def get_sample_metadata_for_project(client: Client, project_puid: str, batch_size=10) -> Generator:
    """
    """

    total_project_samples = get_sample_count_for_project(client, project_puid)
    num_samples_remaining = total_project_samples

    if not total_project_samples:
        log.info(f"Failed to count samples on project: {project_puid}")
        yield []

    sample_metadata_query_variables = {
        'projectId': project_puid,
        'numSamples': batch_size,
    }

    sample_metadata_query = """
query GetSamplePuids($projectId: ID!, $numSamples: Int = 1, $afterCursor: String = null){
  project(puid: $projectId) {
    samples(first: $numSamples, after: $afterCursor){
      totalCount
      pageInfo {
        startCursor
        endCursor
        hasNextPage
      }
      edges {
        node {
          puid
          name
          metadata
        }
        cursor
      }
    }
  }
}
"""
    get_more_sample_metadata = True
    while get_more_sample_metadata:
        sample_metadata_batch = []
        sample_metadata_response = run_query(client, sample_metadata_query, sample_metadata_query_variables)
        page_info = sample_metadata_response.get('project', {}).get('samples', {}).get('pageInfo', {})
        has_next_page = page_info.get('hasNextPage', False)
        get_more_sample_metadata = has_next_page
        end_cursor = page_info.get('endCursor')
        sample_metadata_query_variables['afterCursor'] = end_cursor
        edges = sample_metadata_response.get('project', {}).get('samples', {}).get('edges')
        num_samples_fetched = len(edges)
        num_samples_remaining -= num_samples_fetched
        log.info(f"Fetched metadata for {num_samples_fetched} samples in project: {project_puid}. {num_samples_remaining} project samples remaining to be fetched.")
        for edge in edges:
            sample_metadata = edge.get('node', {}).get('metadata')
            sample_puid = edge.get('node', {}).get('puid')
            sample_name = edge.get('node', {}).get('name')
            if sample_metadata:
                if 'project_puid' not in sample_metadata:
                    sample_metadata['project_puid'] = project_puid
                if 'sample_puid' not in sample_metadata:
                    sample_metadata['sample_puid'] = sample_puid
                if 'sample_name' not in sample_metadata:
                    sample_metadata['sample_name'] = sample_name

                sample_metadata_batch.append(sample_metadata)

        yield sample_metadata_batch


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


def filter_sample_metadata_batch(sample_metadata_batch: list[dict], selection_regexes: list|None=None, exclusion_regexes: list|None=None):
    """
    """
    selected_samples = []
    for sample in sample_metadata_batch:
        sample_name = sample['sample_name']
        regex_used_for_selection = False
        regex_used_for_exclusion = False
        if selection_regexes:
            regex_used_for_selection = is_selected_by_regex(sample_name, selection_regexes)
            log.debug(f"Selected sample: {sample_name} based on regex: {regex_used_for_selection}")
        if exclusion_regexes:
            regex_used_for_exclusion = is_excluded_by_regex(sample_name, exclusion_regexes)
            if regex_used_for_exclusion:
                log.debug(f"Excluded sample: {sample_name} based on regex: {regex_used_for_exclusion}")
                continue
    
        # Exclusion supersedes selection
        if regex_used_for_selection and not regex_used_for_exclusion:
            selected_samples.append(sample)

        # If no regexes supplied for selection, we assume everything
        # that isn't excluded is selected.
        if not selection_regexes:
            selected_samples.append(sample)

    return selected_samples
    

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

    for i in provided_inputs:

        parsed_input = Input(**i)
        merged_inputs_list.append(parsed_input)
            
    return merged_inputs_list


def main(args):
    """
    Download sample attachments via the IRIDA Next GraphQL API.
    """
    if args.input:
        raise NotImplementedError(f"Selecting samples via input file not yet implemented.")
    configure_logging(args.log_level)
    config_dict = {}
    config = None
    inputs = []
    try:
        
        if args.config:
            if not os.path.exists(args.config):
                log.error(f"Config file not found at: {args.config}")
                exit(1)
            config_dict = parse_config(args.config)
            config = merge_config_with_args(args, config_dict)
            inputs = parse_input_file(config.input)
    except Exception as e:
        log.error(f"Failed to load config at: {args.config}. Error: {str(e)}")
        exit(1)

    # print(json.dumps(asdict(config), indent=2, cls=EnhancedJSONEncoder))
    # exit()

    log.info("Configuration complete.")

    try:
        

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

    except Exception as e:
        log.error(f"Failed to connect to IRIDA: {str(e)}" )
        exit(1)

    project_puids = []
    required_fieldnames = ['project_puid', 'sample_puid', 'sample_name']
    all_fieldnames = set()
    all_metadata = []
    num_samples_excluded_by_regex = {}
    num_samples_selected_by_regex = {}
    try:
        if args.group_puid:
            project_puids = get_project_puids_for_group(client, args.group_puid)
        elif args.project_puid:
            project_puids = [args.project_puid]

        for project_puid in project_puids:
            total_project_samples = get_sample_count_for_project(client, project_puid)
            log.info(f"Fetching metadata for {total_project_samples} samples on project: {project_puid}, in batches of {args.batch_size}")
            for sample_metadata_batch in get_sample_metadata_for_project(client, project_puid, batch_size=args.batch_size):
                sample_metadata_batch_size = len(sample_metadata_batch)
                filtered_metadata_batch = filter_sample_metadata_batch(sample_metadata_batch, selection_regexes=config.select_by_regex, exclusion_regexes=config.exclude_by_regex)
                filtered_metadata_batch_size = len(filtered_metadata_batch)
                num_samples_filtered_out = sample_metadata_batch_size - filtered_metadata_batch_size

                if num_samples_filtered_out > 0 and sample_metadata_batch_size > 0:
                    percent_filtered_out = num_samples_filtered_out / sample_metadata_batch_size * 100
                    log.info(f"Filtered out {num_samples_filtered_out}/{sample_metadata_batch_size} ({percent_filtered_out:.1f}%) samples from project {project_puid} based on filtering criteria.")
                all_metadata += filtered_metadata_batch
                for sample_metadata in filtered_metadata_batch:
                    all_fieldnames.update(sample_metadata)
    except Exception as e:
        log.error(f"Error during metadata fetching: {str(e)}")

    if hasattr(signal, "SIGPIPE"):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    try:
        output_fieldnames = required_fieldnames 
        if args.fieldnames_list:
            selected_fieldnames = []
            with open(args.fieldnames_list, 'r') as f:
                for line in f:
                    line_stripped = line.strip()
                    if line_stripped not in selected_fieldnames and line_stripped not in required_fieldnames:
                        selected_fieldnames.append(line_stripped)
            output_fieldnames += selected_fieldnames
        else:
            output_fieldnames += list(all_fieldnames - set(required_fieldnames))
        
        if args.output:
            out_f = open(args.output, 'w')
        else:
            out_f = sys.stdout
        writer = csv.DictWriter(out_f, fieldnames=output_fieldnames, delimiter='\t', extrasaction='ignore', lineterminator='\n', quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for row in all_metadata:
            writer.writerow(row)
    except BrokenPipeError as e:
        log.debug(f"{str(e)}")
    except Exception as e:
        log.error(f"Error writing output: {str(e)}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', type=Path, default=Path.home() / '.config' / 'irida-next-client' / 'config.json', help="""
Optional JSON config file. Can be used to provide "api_base_url", "email", "token", "outdir", using those keys. (default: "~/.config/irida-next-client/config.json")
""")
    parser.add_argument('--api-base-url', default='https://irida-next.nml-lnm.phac-aspc.gc.ca/api/graphql')
    parser.add_argument('-e', '--email', help="email address for user whose access token is used")
    parser.add_argument('-t', '--token', help="IRIDA Next access token (see: https://phac-nml.github.io/irida-next/docs/extend/graphql#authentication-with-personal-access-tokens)")
    parser.add_argument('--log-level', default="info", choices=['debug', 'info', 'warning', 'error'])
    parser.add_argument('-i', '--input', help="""CSV input file with header either 'sample_puid' or 'sample_name'.""")
    parser.add_argument('--batch-size', type=int, default=20, help="Number of samples to fetch in one batch (default: 20)")
    parser.add_argument('--group-puid', help="IRIDA Group PUID. Required if --project-puid not supplied.")
    parser.add_argument('--project-puid', help="IRIDA Project PUID. Required if --group-puid not supplied.")
    parser.add_argument('--select-by-regex', nargs='*', help="Select samples by regex match against sample_name (exclusion supersedes selection).")
    parser.add_argument('--exclude-by-regex', nargs= '*', help="Exclude samples by regex match against sample_name (exclusion supersedes selection).")
    parser.add_argument('--fieldnames-list', type=Path, help="File with a list of metadata fieldnames to include in output")
    parser.add_argument('-o', '--output', type=Path, help="File to write metadata to, or stdout if not supplied.")
    parser.add_argument('--output-delimiter', type=str, default=',', choices=[',', '\t', '|'], help="Output delimiter (default: ',')")
    args = parser.parse_args()

    configure_logging(args.log_level)
    try:
        main(args)
    except Exception as e:
        log.error(f"{str(e)}")
        exit(-1)
