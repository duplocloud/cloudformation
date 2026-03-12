# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial release of the DuploCloud CloudFormation custom resource provider.
- `Custom::Duplo@<Kind>` ResourceType convention mapping to duploctl resources.
- CFN lifecycle handling: Create (apply + wait), Update (apply + wait), Delete.
- Ad-hoc Lambda invocation mode (pipe-style) controlled by `DUPLO_ADHOC_ENABLED`.
- `get_id()` utility generating deterministic CFN physical resource IDs.
- Reserved CFN properties: `Tenant`, `Wait`, `Validate`, `Query`, `AllowImport`, `Body`.
- Multi-stage Dockerfile targeting `public.ecr.aws/lambda/python:3.13`.
- Lambda ZIP build workflow for ZIP-based deployments.
- GitHub Actions workflows: test, pull_request, image, package, publish.
