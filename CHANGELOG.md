# Changelog

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## Unreleased

### Added
 - `DEFAULT_WIKIBASE_URL` environment variables to set the wikibase (Default: 'https://wikidata.org')
 - `WHITELISTED_USERS` is a comma-separated list of usernames that
   will always be allowed to submit batches, even if not autoconfirmed (Default: empty list)
 - Placeholders to refer to new items created in the same batch (#202):
   `CREATE|{Person_A}` defines `{Person_A}`, which later commands can use as entity
   or value. In CSV, a `qid` cell like `{Person_A}` creates an item with that placeholder.
   Placeholders must be defined before they are used.

### Fixed
 - A `LAST` command following a combined item creation that it could not be combined with
   now refers to the created item instead of failing.

### Removed
 - `BASE_REST_URL` is no longer supported. Use `DEFAULT_WIKIBASE_URL` instead.
