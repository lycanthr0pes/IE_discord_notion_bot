# Fork and Upstream Sync Workflow

## One-time setup

```bash
git remote add upstream https://github.com/lycanthr0pes/IE_Event_Bot.git
```

## Before starting feature work

```bash
git fetch upstream
git checkout develop
git rebase upstream/develop
git push origin develop
```

## Branch rules

- `feature/*` must open PR to `develop`
- `release/*` must open PR to `main`
- `hotfix/*` must open PR to `main`
- `sync/*` must open PR to `develop`

## Release flow

1. Create `release/x.y.z` from `develop`.
2. Open PR `release/x.y.z -> main`.
3. Merge PR. Release Please opens a release PR.
4. Merge release PR to create tag `vX.Y.Z` and GitHub Release.
5. Auto sync PR `main -> develop` is created by workflow.

