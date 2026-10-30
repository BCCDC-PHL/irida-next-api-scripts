#!/usr/bin/env python

import argparse
import base64
import csv
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import time
import uuid

from collections.abc import Generator
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path
from typing import Any

from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport
from gql.transport.aiohttp import AIOHTTPTransport
from gql.transport.exceptions import TransportQueryError, TransportServerError

import requests


RETRYABLE = (
    TransportServerError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)

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
    file: Path
    project_puid: str|None = None
    sample_name: str|None = None
    sample_puid: str|None = None
    input: Path|None = None


@dataclass
class Input:
    """
    One record from the input, representing a
    sample for which attachments should be uploaded.
    """
    file: os.PathLike
    sample_name: str
    sample_puid: str
    project_puid: str


@dataclass
class Attachment:
    """
    One attachment record, as received from the IRIDA API
    """
    puid: str
    attachment_url: str
    filename: str
    byte_size: int

    
@dataclass
class DirectUpload:
    """
    One directUpload record, with fields merged from local infor and as received from the IRIDA API
    """
    filename: str
    size_bytes: int
    checksum: str
    url: str
    headers: dict
    blob_id: str
    signed_blob_id: str
    mutation_id: str


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

            if 'file' in parsed_input:
                parsed_input['file'] = Path(parsed_input['file'])

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


def run_query(
    client: Client,
    query_str: str,
    variable_values: dict | None = None,
    max_retries: int = 0,
    backoff_seconds: float = 2.0,
) -> dict:
    """
    Run a GraphQL query or mutation. Retries only on transient transport
    failures; GraphQL-level errors are logged and re-raised immediately.
    """
    document = gql(query_str)
    attempt = 0
    while True:
        try:
            return client.execute(document, variable_values=variable_values or {})
        except TransportQueryError as e:
            for error in e.errors or []:
                log.error(f"GraphQL error: {error.get('message', '')} "
                          f"(path={error.get('path')})")
            raise
        except RETRYABLE as e:
            if attempt >= max_retries:
                log.error(f"Giving up after {attempt + 1} attempt(s): {e}")
                raise
            attempt += 1
            delay = backoff_seconds * 2 ** (attempt - 1)
            log.warning(f"Transient error ({e}); retry {attempt}/{max_retries} in {delay:.0f}s")
            time.sleep(delay)


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
        id
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


def create_direct_upload(client: Client, file_path: os.PathLike) -> DirectUpload:
    """
    Download an attachment to an output dir.
    """
    mutation_id = str(uuid.uuid4())
    file_size_bytes = os.path.getsize(file_path)
    filename = os.path.basename(file_path)

    digest_obj = None
    with open(file_path, "rb") as f:
        digest_obj = hashlib.file_digest(f, "md5")
    md5_checksum = digest_obj.digest()
    md5_checksum_base64 = base64.b64encode(md5_checksum).decode("utf-8")

    content_type = mimetypes.guess_type(Path(file_path).name)[0] or "application/octet-stream"

    query_variable_values = {
        "input": {
            'clientMutationId': mutation_id,
            'byteSize': file_size_bytes,
            'checksum': md5_checksum_base64,
            'filename': filename,
            'contentType': content_type,
        }
    }

    query = """
mutation CreateDirectUpload($input: CreateDirectUploadInput!){
  createDirectUpload(input: $input){
    clientMutationId
    directUpload {
      url
      headers
      blobId
      signedBlobId
    }
  }   
}
"""
    create_direct_upload_response = run_query(client, query, query_variable_values)
    if not create_direct_upload_response:
        raise RuntimeError(f"Failed to create direct upload for file: ${filename}")
    
    payload = create_direct_upload_response["createDirectUpload"]
    received_mutation_id = payload.get("clientMutationId")
    if received_mutation_id != mutation_id:
        raise RuntimeError(f"clientMutationId mismatch in createDirectUpload response. Sent: {mutation_id}, Recieved: {received_mutation_id}.")
    else:
        log.info(f"Created direct upload with mutation id: {mutation_id}")

    du_dict = {
        'mutation_id': mutation_id,
        'filename': filename,
        'size_bytes': file_size_bytes,
        'checksum': md5_checksum,
        'url': payload['directUpload']['url'],
        'headers': json.loads(payload['directUpload']['headers']),
        'blob_id': payload['directUpload']['blobId'],
        'signed_blob_id': payload['directUpload']['signedBlobId'],
    }

    direct_upload = DirectUpload(**du_dict)

    return direct_upload


def upload_file(direct_upload: DirectUpload, file_path: os.PathLike) -> DirectUpload:
    """
    Upload a file to a pre-signed url
    """
    with open(file_path, "rb") as f:
        resp = requests.put(direct_upload.url, data=f, headers=direct_upload.headers)
    resp.raise_for_status()

    return direct_upload


def attach_files_to_sample(client: Client, sample_puid: str, uploaded_files: list[DirectUpload]) -> dict:
    """
    Attach uploaded files to a sample by their signed blob IDs.
    Must create a direct upload and upload file before attaching file to sample.
    """
    mutation_id = str(uuid.uuid4())

    signed_blob_ids = [f.signed_blob_id for f in uploaded_files]
    query_variable_values = {
        "input": {
            'clientMutationId': mutation_id,
            'files': signed_blob_ids,
            'samplePuid': sample_puid,
        }
    }

    query = """
mutation AttachFilesToSample($input: AttachFilesToSampleInput!){
  attachFilesToSample(input: $input){
    clientMutationId
    sample {
      id
      puid
      name
      attachmentsUpdatedAt
      attachments {
        totalCount
      }
    }
    status
    errors {
      message
      path
    }
  }   
}
"""
    response = run_query(client, query, query_variable_values)
    if not response:
        raise RuntimeError(f"Failed to attach files for sample: {sample_puid}")
    payload = response["attachFilesToSample"]
    received_mutation_id = payload.get("clientMutationId")

    if 'errors' not in payload:
        log.error(f"Malformed response while attaching files to sample. keys: {payload.keys()}")
        raise RuntimeError(f"Error attaching files to sample: {sample_puid}")
    if len(payload['errors']) > 0:
        for e in payload['errors']:
            if e.get('message') == 'checksum matches existing file':
                log.warning(f"Duplicate file upload for {sample_puid}: {e}")
                continue
            else:
                log.error(f"{e}")
                raise RuntimeError(f"Error attaching files to sample: {sample_puid}")

    if received_mutation_id != mutation_id:
        raise RuntimeError(f"clientMutationId mismatch in attachFilesToSample response. Sent: {mutation_id}, Recieved: {received_mutation_id}.")
    else:
        log.info(f"Attached files to sample: {sample_puid} with mutation id: {mutation_id}")

    return payload


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
    if args.file:
        merged_config['file'] = args.file

    valid_config_fields = {f.name for f in fields(Config)}
    filtered_merged_config_dict = {k: v for k, v in merged_config.items() if k in valid_config_fields}

    parsed_config = Config(**filtered_merged_config_dict)

    return parsed_config


def merge_inputs_with_config(provided_inputs: list, config: Config) -> list[dict]:
    """
    Merge input file with config info.
    """
    merged_inputs_list = []

    for i in provided_inputs:
        if not i.get('project_puid'):
            i['project_puid'] = config.project_puid

        if not i.get('sample_name'):
            i['sample_name'] = config.sample_name
        
        if not i.get('sample_puid'):
            sample_name = i.get('sample_name')
            project_puid = i.get('project_puid')
            if not all([sample_name, project_puid]):
                log.error(f"Both sample_name and project_puid are required if sample_puid is not supplied. sample_name: {sample_name}, project_puid: {project_puid}")
                continue

        merged_inputs_list.append(i)

    return merged_inputs_list


def check_inputs(client: Client, inputs: list[dict]) -> dict[str, list[Input]]:
    """
    """
    inputs_by_sample_puid = {}
    for i in inputs:
        sample_puid = i.get('sample_puid')
        if not sample_puid:
            project_puid = i.get('project_puid')
            sample_name = i.get('sample_name')
            file = i.get('file')
            if not all([project_puid, sample_name]):
                log.error(f"Both Project PUID and Sample Name are required if Sample PUID not supplied. Project PUID: {project_puid}, Sample Name: {sample_name}, file: {file}")
                continue
            sample_puid = get_sample_puid(client, project_puid, sample_name)
            i['sample_puid'] = sample_puid
        if not sample_puid:
            log.error(f"Error obtaining Sample PUID for Sample: {sample_name} on Project: {project_puid}")
            continue

        file_path = i.get('file')
        if not file_path or not os.path.exists(file_path):
            log.error(f"File: {file_path} does not exists for Sample: {sample_name} on Project: {project_puid}")
            continue

        if sample_puid not in inputs_by_sample_puid:
            inputs_by_sample_puid[sample_puid] = []
        input_obj = Input(**i)
        inputs_by_sample_puid[sample_puid].append(input_obj)

    return inputs_by_sample_puid
        


def main(args):
    """
    Upload sample attachments via the IRIDA Next GraphQL API.
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
    else:
        if not args.file:
            log.error(f"File must be supplied via '--file' flag if input csv file not supplied.")
            exit(1)
        inputs = [{
            'file': args.file,
        }]

    input_dicts = merge_inputs_with_config(inputs, config)

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

    inputs_by_sample_puid = check_inputs(client, input_dicts)

    for sample_puid, inputs in inputs_by_sample_puid.items():
        uploaded_files = []
        for i in inputs:
            log.info(f"Creating direct upload for sample {i.sample_name} on project {i.project_puid}")
            direct_upload = None
            try:
                direct_upload = create_direct_upload(client, i.file)
            except Exception as e:
                log.error(f"Error creating direct upload for file: {i.file} for sample: {i.sample_name} on project: {i.project_puid}: {str(e)}")
                continue
            uploaded_file = upload_file(direct_upload, i.file)
            log.info(f"Uploaded file: {uploaded_file.filename} for sample: {i.sample_name} on project: {i.project_puid}")
            uploaded_files.append(uploaded_file)

        attach_files_to_sample(client, sample_puid, uploaded_files)
        log.info(f"Attached {len(uploaded_files)} files to sample: {sample_puid}")
        

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', type=Path, default=Path.home() / '.config' / 'irida-next-client' / 'config.json', help="""
Optional JSON config file. Can be used to provide "api_base_url", "email", "token", "outdir", using those keys. (default: "~/.config/irida-next-client/config.json")
""")
    parser.add_argument('--api-base-url', default='https://irida-next.nml-lnm.phac-aspc.gc.ca/api/graphql')
    parser.add_argument('-e', '--email', help="email address for user whose access token is used")
    parser.add_argument('-t', '--token', help="IRIDA Next access token (see: https://phac-nml.github.io/irida-next/docs/extend/graphql#authentication-with-personal-access-tokens)")
    parser.add_argument('-i', '--input', help="""
CSV input file with headers "project_puid", "sample_puid", "sample_name", "file".
If "sample_puid" is not supplied, then "project_puid" and "sample_name" are both required.
If "project_puid" is not supplied in input file, then it is required via "--project-puid" flag.
""")
    parser.add_argument('--project-puid', help="IRIDA Project PUID. Required if not supplied via input csv file.")
    parser.add_argument('--sample-puid', help="IRIDA Sample PUID.")
    parser.add_argument('--sample-name', help="IRIDA Sample Name. Project PUID required if Sample PUID is not supplied")
    parser.add_argument('-f', '--file', type=Path, help="File to upload")
    args = parser.parse_args()
    main(args)
