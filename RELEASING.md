# Releasing edgecut

The release workflow builds a source distribution and a wheel, checks their
metadata and installation, and runs the FFmpeg tests on Python 3.9 and 3.14.
Pushes to `main` and pull requests run these checks without publishing.

## Trusted Publishing setup

The workflow uses PyPI Trusted Publishing, without a stored API token. Configure
the GitHub environment `pypi` and the following publisher on PyPI:

| Field | Value |
| --- | --- |
| PyPI project name | `edgecut` |
| Owner | `ZeroMarker` |
| Repository | `edgecut` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

For the first release, add a pending publisher at
<https://pypi.org/manage/account/publishing/>. After the first successful upload,
PyPI creates the project and converts the pending publisher automatically.

## Publish a version

1. Set the same version in `pyproject.toml` and `edgecut.py`.
2. Commit and push to `main`, and wait for the workflow to pass.
3. Create and publish a GitHub Release targeting that commit, with a tag matching
   the package version exactly, for example `v0.1.0`. Publishing a non-prerelease
   GitHub Release triggers the PyPI upload; pushing a tag alone does not.
4. Confirm the workflow succeeds and the version appears at
   <https://pypi.org/project/edgecut/>. Test installation with
   `uv tool install edgecut==0.1.0` (substitute the released version).

PyPI does not allow an uploaded filename/version to be reused. Fix a broken
release by preparing a new version.

## Local checks

```sh
python -m pip install build twine
python -m unittest discover -s tests -v
python -m build
python -m twine check --strict dist/*
```
