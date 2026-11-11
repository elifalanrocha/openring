# Releasing OpenRing

A release publishes the Python package to [PyPI](https://pypi.org/project/openring/) and the `npx openring`
launcher to [npm](https://www.npmjs.com/package/openring). The launcher installs the PyPI release with
its own version number, so both always ship together.

## Every release

1. Set the same version in `pyproject.toml`, `openring/__init__.py` and `package.json`
   (`tests/test_release.py` fails if they differ).
2. Add the changes to `CHANGELOG.md` and commit.
3. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`.
   `.github/workflows/release.yml` checks that the tag matches the versions, runs the tests, publishes
   to PyPI and, once that succeeds, stages the release on npm.
4. Approve the staged release on npmjs.com (the package's **Staged Packages** tab) with two-factor
   authentication. Until then `npx openring` keeps installing the previous version.

   The same tag starts `.github/workflows/docker.yml`, which pushes `ghcr.io/elifalanrocha/openring` (`latest`,
   the version and the minor). It needs no setup except one: after the first push, open the package under the
   repository's **Packages** and set its visibility to public, so `docker pull` works without a login.

Both registries use trusted publishing: they trust this repository's workflow directly, so no tokens are
stored anywhere. npm only stages, so a stolen GitHub login alone can't publish a new version.

## One-time setup

### PyPI

Before the first release, register the workflow as a *pending publisher*
(PyPI → your account → Publishing → Add a new pending publisher → GitHub):

| Field | Value |
| --- | --- |
| PyPI project name | `openring` |
| Owner | `elifalanrocha` |
| Repository name | `openring` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

The first tagged release creates the project on PyPI.

### npm

npm only accepts a trusted publisher for a package that already exists, so the first version is
published by hand, right after the first PyPI release succeeds (the workflow skips npm with a warning
until then):

```bash
npm login                    # the account needs two-factor authentication to publish
npm publish --access public  # a new package can take a minute or two to appear
```

Then trust the workflow with permission to *stage* releases only, **at most 48 hours before the next
release**: npm drops a trusted publisher
that no publish has used within 48 hours of its creation. It needs npm 11.15 or later (`npx npm@latest`
gets it), and asks for two-factor authentication:

```bash
npx npm@latest trust github openring --file release.yml --repository elifalanrocha/openring --environment npm
npx npm@latest trust list openring
```

Push the release tag right after; that first automated publish validates the trust, and from then on
tags publish to npm automatically. (The trusted publisher form on npmjs.com may not save; the command
above does the same.)
