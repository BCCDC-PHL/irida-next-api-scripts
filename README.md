# IRIDA Next API Scripts

Helper scripts for performing common tasks with the IRIDA Next API.

## Setup and Configuration

### Setup

The scripts depend on the [gql](https://github.com/graphql-python/gql) library for interacting with the IRIDA Next [GraphQL](https://graphql.org) API from python.

Create a conda environment that includes these dependencies using the provided `environment.yml` file:

```
conda env create -f environment.yml
```

...and activate it prior to running the scripts:

```
conda activate irida-next-api
```

### Configuration

The scripts accept a JSON-formatted configuration file that can be used to supply common arguments:

```json
{
    "api_base_url": "https://irida-next.nml-lnm.phac-aspc.gc.ca/api/graphql",
    "email": "you@example.org",
    "token": "yoUrTokEn-"
}
```

By default, the scripts will look for this config file in `~/.config/irida-next-api-scripts/config.json`. A config file can also be supplied directly via the `-c` or `--config` flags.

## Scripts

### download-sample-attachments

Download files attached to samples.

#### Usage

```
usage: download-sample-attachments.py [-h] [-c CONFIG] [--api-base-url API_BASE_URL] [-e EMAIL] [-t TOKEN] [-i INPUT] [--project-puid PROJECT_PUID] [--sample-puid SAMPLE_PUID]
                                      [--sample-name SAMPLE_NAME] [-o OUTDIR]

options:
  -h, --help            show this help message and exit
  -c, --config CONFIG   Optional JSON config file. Can be used to provide "api_base_url", "email", "token", "outdir", using those keys. (default: "~/.config/irida-next-
                        client/config.json")
  --api-base-url API_BASE_URL
  -e, --email EMAIL     email address for user whose access token is used
  -t, --token TOKEN     IRIDA Next access token (see: https://phac-nml.github.io/irida-next/docs/extend/graphql#authentication-with-personal-access-tokens)
  -i, --input INPUT     CSV input file with headers "project_puid", "sample_puid", "sample_name". If "sample_puid" is not supplied, then "project_puid" and "sample_name" are both
                        required. If "project_puid" is not supplied in input file, then it is required via "--project-puid" flag.
  --project-puid PROJECT_PUID
                        IRIDA Project PUID. Required if not supplied via input csv file.
  --sample-puid SAMPLE_PUID
                        IRIDA Sample PUID.
  --sample-name SAMPLE_NAME
                        IRIDA Sample Name. Project PUID required if Sample PUID is not supplied
  -o, --outdir OUTDIR   Base outdir to download attachments to. Subdirectories will be created for each sample.
```

##### Examples

These examples assume that you have a config file at `~/.config/irida-next-api-scripts/config.json` that includes the `"api_base_url"`, `"email"` and `"token"` settings.

Download the attachments for a single sample:

```
download-sample-attachments.py \
  --sample-name "MYSAMPLE" \
  --project-puid "INXT_PRJ_ABC123ABC1" \
  --outdir downloaded_irida_attachments
```

Downloading attachments for multiple samples on the same project:

A CSV input file may be supplied for downloading attachments for multiple samples. When downloading multiple samples from one project, the Project PUID can be supplied via the `--project-puid` flag, and the input csv file can include the Sample Name for multiple samples:

```csv
sample_name
MYSAMPLE-01
MYSAMPLE-02
MYSAMPLE-03
```

```
download-sample-attachments.py \
  --input /path/to/input.csv \
  --project-puid "INXT_PRJ_ABC123ABC1" \
  --outdir /path/to/downloaded_irida_attachments
```

Downloading attachments for samples on multiple projects:

When downloading attachments for samples from multiple projects, you can supply the Project PUID for each sample via the input csv file:

```csv
sample_name,project_puid
MYSAMPLE-01,INXT_PRJ_ABC123ABC1
MYSAMPLE-02,INXT_PRJ_DEF456DEF2
MYSAMPLE-03,INXT_PRJ_GHI789GHI3
```

Alternatively, the Sample PUIDs can be supplied directly:

```csv
sample_puid
INXT_SAM_ABC123ABC1
INXT_SAM_DEF456DEF2
INXT_SAM_GHI789GHI3
```

When multple Project PUIDs are used, the `--project-id` flag can be omitted:

```
download-sample-attachments.py \
  --input /path/to/input.csv \
  --outdir /path/to/downloaded_irida_attachments
```

