# Contributing

## Taking an issue

Comment `/claim` on an issue to take it. `/unclaim` or `/release` gives it
back. pr-gate refuses a pull request unless an issue it references is assigned
to its author.

## Commit messages

Every commit message follows Conventional Commits: `type(scope)!: subject`.
The scope and the `!` are optional. The type is one of `build`, `chore`, `ci`,
`docs`, `feat`, `fix`, `perf`, `refactor`, `revert`, `style` or `test`. A
breaking change carries the `!`. A message an automation writes follows the
same format.

pr-gate checks the format of the subject of every commit in a pull request,
merge commits excepted. Review checks that each type and `!` fit the change. A
wrong message is reworded before the merge.

## Pull requests

Describe the change in the sections of `.github/PULL_REQUEST_TEMPLATE.md`.
pr-gate checks the body against that template, so delete the template's
instruction comments and every conditional or optional section that does not
apply.

The README's Development section lists the commands that run the tests and the
lint, type and workflow checks.

## Licence

A contribution is made under the repository's MIT licence (`LICENSE`), as
section D.6 of GitHub's Terms of Service provides for content added to a
repository that carries a licence. The project asks for no DCO sign-off and no
contributor licence agreement.

## Security

Report a vulnerability as `SECURITY.md` describes, not in a public issue.
