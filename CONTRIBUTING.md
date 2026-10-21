# Contributing to Glovebox

Thanks for helping improve Glovebox! Contributions of all sizes are welcome.

## Guidelines

- **Open an issue first** for large changes or new features so we can agree on scope.
- **Keep it small and focused.** One logical change per pull request; avoid unrelated refactors or formatting churn.
- **Match the project's style.** Python (FastAPI + SQLite) on the backend, vanilla JS with no build step on the frontend.
- **Add or update tests** for behavior changes, and make sure they pass:
  ```bash
  pip install -r requirements.txt pytest httpx
  python -m pytest tests/ -q
  ```
- **Preserve user data.** Schema changes must be new, additive migrations in `app/db.py`. Never edit an existing migration.
- **Be respectful** in issues and reviews.

## License and Contributor License Agreement (CLA)

Glovebox is licensed under the
[PolyForm Noncommercial License 1.0.0](LICENSE.md). It is free for noncommercial
use; commercial use requires a separate license from the maintainer.

By submitting a contribution (for example, a pull request), you agree that:

1. You wrote the contribution, or have the right to submit it.
2. You license your contribution to the project and its users under the
   PolyForm Noncommercial License 1.0.0.
3. You grant Daniel Phillips, the project maintainer, a perpetual, worldwide,
   non-exclusive, royalty-free, irrevocable license to use, modify, sublicense,
   and relicense your contribution as part of Glovebox, including under other
   open-source, source-available, or commercial licenses, and to sell or
   otherwise commercially license Glovebox with your contribution included.

If you do not agree, please do not submit a contribution.
