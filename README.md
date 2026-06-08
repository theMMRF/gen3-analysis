# Gen3 Analysis API

![version](https://img.shields.io/github/release/uc-cdis/gen3-analysis.svg) [![Apache license](http://img.shields.io/badge/license-Apache-blue.svg?style=flat)](LICENSE) [![Coverage Status](https://coveralls.io/repos/github/uc-cdis/gen3-analysis/badge.svg?branch=main)](https://coveralls.io/github/uc-cdis/gen3-analysis?branch=main)

## Overview

The Gen3 Analysis service allows users to run analysis queries on Gen3 data.

## Getting Started

### Installation
assume you have poetry installed:
```bash
poetry install
```

### Running the server
```bash
poetry run uvicorn gen3analysis.main:app_instance --reload
```
Note: the --reload flag is optional and will reload the server on code changes

## Details

The server is built with [FastAPI](https://fastapi.tiangolo.com/) and packaged with [Poetry](https://poetry.eustace.io/).

- Use `bin/run.sh` to spin up a `localhost` instance of the API
- Use `bin/test.sh` to run all the tests
- Use `bin/clean.sh` to run several formatting and linting commands

## Key documentation

The documentation can be browsed in the [docs](docs) folder, and key documents are linked below.

* [Detailed API Documentation](http://petstore.swagger.io/?url=https://raw.githubusercontent.com/uc-cdis/gen3-analysis/main/docs/openapi.yaml)
* [Quickstart](docs/quickstart.md)
* [Terms & Conditions acceptance](docs/terms_acceptance.md) — schema, API, and deployed environment setup
