# Contributing to Engineering Platform

Use an isolated Python environment and run the full standalone suite with
`PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'`.
Browser qualification uses `npm ci` followed by `npm run test:engineering-dashboard`.
Run the extraction audit when changing extracted source, tests, docs, or
workflows. Do not permit a source checkout to access or become production
authority; use temporary installation and data roots for all qualification.

Report security concerns through [SECURITY.md](SECURITY.md), never in public
issues with secrets or production evidence.
