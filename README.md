# Human-Approved Observation Outreach

A privacy-clean reference workflow for preparing an observation-placement request without automating contact:

- local SQLite lead/touchpoint state;
- explicit approval and deterministic IDs;
- official-source and public-contact validation;
- opt-out, refusal, bounce and reply states;
- drafts only — no Gmail, Discord or external-send adapter is included.

The production profile, student identity, school, dates, contacts and private integrations were intentionally removed. Adapt the templates to the legal and safeguarding requirements of your jurisdiction.

## Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
```
