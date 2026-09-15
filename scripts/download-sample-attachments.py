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
    outdir: Path
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


@dataclass
class Attachment:
    """
    One attachment record, as received from the IRIDA API
    """
    puid: str
    attachment_url: str
    filename: str
    byte_size: int


def parse_input_file(input_path: Path) -> list[dict]:
    """
    Parse and input (samplesheet) file.

    Expected fields include:
    sample_puid
    sample_name
    project_puid
    outdir
    """
    parsed_inputs = []

    with open(input_path, 'r') as f:
        reader = csv.DictReader(f, delimiter=',')
        for row in reader:
            parsed_input = {}
            for k, v in row.items():
                k = k.replace(' ', '_').lower()
                parsed_input[k] = v

            if 'outdir' in parsed_input:
                parsed_input['outdir'] = Path(parsed_input['outdir'])

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


def get_num_sample_attachments(client: Client, sample_puid: str) -> int|None:
    """
    Given an IRIDA Sample PUID, return the number of attachments on the sample.
    """
    num_attachments = None
    query = """
query GetSampleAttachmentsTotalCount($sampleId: ID!){
  sample(puid: $sampleId){
    attachments{
      totalCount
    }
  }
}
"""
    query_variables = {
        'sampleId': sample_puid,
    }

    response = run_query(client, query, query_variables)
    if not response:
        log.error(f"Failed to get num attachments for sample with puid: {sample_puid}")
        return None

    num_attachments = response.get('sample', {}).get('attachments', {}).get('totalCount')

    return num_attachments


def get_sample_attachments(client: Client, sample_puid: str) -> Generator[Attachment, None, None]:
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
query GetSampleAttachments($sampleId: ID!, $numAttachments: Int = 1, $afterCursor: String = null){
  sample(puid: $sampleId){
    attachments(first: $numAttachments, after: $afterCursor){
      edges{
        node{
          puid,
          filename,
          attachmentUrl,
          byteSize
        }
        cursor
      }
      pageInfo {
        startCursor
        endCursor
        hasPreviousPage
        hasNextPage
      }
    }
  }   
}
"""

    get_more_attachments = True
    num_retries = 0
    max_retries = 5
    while get_more_attachments:
        sample_attachments_response = run_query(client, query, query_variable_values)
        if not sample_attachments_response:
            log.error(f"Failed to get attachments for sample {sample_puid}")
            num_retries += 1
            if num_retries >= max_retries:
                get_more_attachments = False
            continue
        else:
            num_retries = 0

        attachment_edges = sample_attachments_response.get('sample', {}).get('attachments', {}).get('edges', [])
        attachment_page_info = sample_attachments_response.get('sample', {}).get('attachments', {}).get('pageInfo')
        has_next_page = attachment_page_info.get('hasNextPage')
        if has_next_page is False:
            get_more_attachments = False
        if not attachment_page_info:
            log.error(f"Failed to get page info for sample {sample_puid} at cursor: {cursor}")
            num_retries += 1
            if num_retries >= max_retries:
                get_more_attachments = False
            continue
        else:
            num_retries = 0

        for attachment_edge in attachment_edges:
            cursor = attachment_edge.get('cursor')
            query_variable_values['afterCursor'] = cursor
            attachment_node = attachment_edge.get('node')
            if attachment_node:
                attachment_dict = {
                    'puid': attachment_node.get('puid'),
                    'filename': attachment_node.get('filename'),
                    'attachment_url': attachment_node.get('attachmentUrl'),
                    'byte_size': attachment_node.get('byteSize'),
                }


                attachment = Attachment(**attachment_dict)
                yield attachment


def download_sample_attachment(attachment: Attachment, outdir: Path):
    """
    Download an attachment to an output dir.
    """
    os.makedirs(outdir, exist_ok=True)

    
    log.debug(f"url: {attachment.attachment_url}")
    destination_path = os.path.join(outdir, attachment.filename)
    logging.info(f"Download started:   {attachment.filename} ({attachment.byte_size:,} bytes)")
    with requests.get(attachment.attachment_url, stream=True) as response:
        response.raise_for_status() # Raise an error for bad responses (404, 500)
        with open(destination_path, 'wb') as file:
            for chunk in response.iter_content(chunk_size=8192):
                file.write(chunk)

    downloaded_file_size = os.path.getsize(destination_path)
    logging.info(f"Download completed: {attachment.filename} ({downloaded_file_size:,} bytes)")


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
    if args.outdir:
        merged_config['outdir'] = args.outdir
    else:
        outdir = config.get('outdir')
        if not outdir:
            log.error(f"Output directory ('-o', '--outdir') is required if not supplied in config")
            exit(1)
        merged_config['outdir'] = Path(outdir)

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
        if re.search(regex, filename):
            log.debug(f"Selecting file {filename} based on regex '{regex}'")
            return regex
    return False


def is_excluded_by_regex(filename: str, excluded_regexes: list[str] = []):
    """
    Check if an attachment is excluded, based on its filename
    and the supplied regexes.
    """
    for regex in excluded_regexes:
        if re.search(regex, filename):
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

    os.makedirs(config.outdir, exist_ok=True)

    for i in inputs:
        project_puid = i.project_puid
        sample_name = i.sample_name
        sample_puid = i.sample_puid
        if not i.sample_puid:
            if not all([i.project_puid, i.sample_name]):
                continue
            sample_puid = get_sample_puid(client, project_puid=i.project_puid, sample_name=i.sample_name)
            if not sample_puid:
                log.error(f"Failed to fetch PUID for sample {sample_name} on project {project_puid}")
                continue

        num_sample_attachments = get_num_sample_attachments(client, sample_puid=sample_puid)
        num_sample_attachments_downloaded = 0
        num_sample_attachments_excluded_by_regex = {}
        num_sample_attachments_selected_by_regex = {}
        log.info(f"Found {num_sample_attachments} attachments for sample {sample_name} (puid: {sample_puid})")

        sample_subdir_name = i.sample_puid
        if i.sample_name:
            sample_subdir_name = i.sample_name
        sample_outdir = Path(os.path.join(config.outdir, sample_subdir_name))
        os.makedirs(sample_outdir, exist_ok=True)
        for sample_attachment in get_sample_attachments(client, sample_puid=sample_puid):
            regex_used_for_exclusion = is_excluded_by_regex(sample_attachment.filename, config.exclude_by_regex)
            regex_used_for_selection = is_selected_by_regex(sample_attachment.filename, config.select_by_regex)

            # Selection supersedes exclusion
            if regex_used_for_selection:
                if regex_used_for_selection in num_sample_attachments_selected_by_regex:
                    num_sample_attachments_selected_by_regex[regex_used_for_selection] += 1
                else:
                    num_sample_attachments_selected_by_regex[regex_used_for_selection] = 1
                download_sample_attachment(sample_attachment, sample_outdir)
                num_sample_attachments_downloaded += 1
                continue

            if regex_used_for_exclusion:
                if regex_used_for_exclusion in num_sample_attachments_excluded_by_regex:
                    num_sample_attachments_excluded_by_regex[regex_used_for_exclusion] += 1
                else:
                    num_sample_attachments_excluded_by_regex[regex_used_for_exclusion] = 1
                continue
            
            if not config.select_by_regex:
                download_sample_attachment(sample_attachment, sample_outdir)
                num_sample_attachments_downloaded += 1
    
        log.info(f"Downloaded {num_sample_attachments_downloaded}/{num_sample_attachments} attachments for sample {sample_name} (puid: {sample_puid}) on project {project_puid}")
        if num_sample_attachments_excluded_by_regex:
            for regex_used_for_selection, num_sample_attachments_selected in num_sample_attachments_selected_by_regex.items():
                log.info(f"Selected    {num_sample_attachments_selected}/{num_sample_attachments} attachments based on regex '{regex_used_for_selection}' for sample {sample_name} (puid: {sample_puid}) on project {project_puid}")
            for regex_used_for_exclusion, num_sample_attachments_excluded in num_sample_attachments_excluded_by_regex.items():
                log.info(f"Excluded    {num_sample_attachments_excluded}/{num_sample_attachments} attachments based on regex '{regex_used_for_exclusion}' for sample {sample_name} (puid: {sample_puid}) on project {project_puid}")

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
    parser.add_argument('--project-puid', help="IRIDA Project PUID. Required if not supplied via input csv file.")
    parser.add_argument('--sample-puid', help="IRIDA Sample PUID.")
    parser.add_argument('--sample-name', help="IRIDA Sample Name. Project PUID required if Sample PUID is not supplied")
    parser.add_argument('--select-by-regex', nargs='*', help="Select attachments by regex match against filename (selection supersedes exclusion).")
    parser.add_argument('--exclude-by-regex', nargs= '*', help="Exclude attachments by regex match against filename (selection supersedes exclusion).")
    parser.add_argument('-o', '--outdir', type=Path, help="Base outdir to download attachments to. Subdirectories will be created for each sample.")
    args = parser.parse_args()
    main(args)
