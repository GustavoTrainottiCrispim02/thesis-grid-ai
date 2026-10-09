# Grid flexibility and AC/DC optimization

Gustavo Trainotti Crispim · undergraduate thesis in progress.

This project quantifies how network-model fidelity changes the modeled operating-cost value of a flexible data center and a transmission rating increase on RTS-GMLC.

## Contents

- `docs/`: complete interactive research website, ready for GitHub Pages.
- `src/`: four-hour, daily, representative-day, and rating-sensitivity research scripts.
- `notebooks/`: uploaded Notebook 3, 4, and 5 snapshots, with execution outputs and nonessential metadata removed.
- `docs/assets/representative-results.zip`: original 27-day result archive, with both power factors.
- `scripts/`: publication checks, Pages configuration, and a dry-run local thesis importer.

This is the material available in the current research workspace. It is not a complete copy of the author's Mac. Other notebooks, local modifications, data-path configuration, and environment versions need to be reconciled before claiming exact end-to-end reproduction.

## Main comparison

27 selected days × four matched cases × two power factors (1.00 and 0.95), evaluated under DC and AC network models. The facility is at bus 309, with a 100 MW baseline, 50–150 MW flexible range, and 2,400 MWh/day. C6's rating is increased by 50%; resistance and reactance do not change.

At PF 1.00, combined selected-day savings are $924,992.03 in DC and $911,567.51 in AC. Aggregate benefit erosion is +1.45%. These are selected-day totals, not annual estimates or investment returns. AC solutions use multistart IPOPT and have no global optimality certificate.

## Run the study

Use Python 3.12 and install the packages in `requirements.txt`. Install an IPOPT executable separately. Download RTS-GMLC from https://github.com/GridMod/RTS-GMLC. Inspect and adapt the notebooks' dataset and solver paths to your machine.

Run Notebook 3, 4, and the setup/daily pilot cells of Notebook 5 as needed for their dependent inputs. In the prepared Notebook 5 kernel, run:

```python
%run -i "../src/ac_dc_representative_days.py"
```

The scripts depend on the prepared notebook namespace. Read their required-input checks and solver configuration first. Do not assume the downloaded scripts run standalone.

## Preview the website locally

```bash
python3 -m http.server 8000 --directory docs
```

Then open http://localhost:8000. With `repository_url: null`, the code link remains forthcoming. GitHub Actions inserts the actual repository URL during deployment. You can set that URL locally using `GITHUB_REPOSITORY=actual-owner/thesis-grid-ai python3 scripts/prepare_pages.py`.

## Publish with GitHub Pages

1. Review the files and run `python3 scripts/check_publication.py`.
2. Create a GitHub repository called `thesis-grid-ai`, and push this curated project to `main`. Start with a private repository if more local files still need review. Never upload the entire Downloads folder or include credentials, environments, solver binaries, or raw notebook outputs.
3. For a public academic site on GitHub Free, use a reviewed public repository. GitHub Pages publishes its website to the internet; a private repository does not automatically make the site private.
4. In the repository, open **Settings → Pages → Source → GitHub Actions**.
5. Open **Actions → Publish research website → Run workflow**, choosing `main`.
6. The published project URL will be `https://YOUR-USERNAME.github.io/thesis-grid-ai/`. Future pushes that change the website trigger an update.

Only `docs/` is uploaded as the Pages website artifact. Python code and notebooks are visible in the repository when it is public but are not copied into the website artifact. No API tokens are placed in the workflow or frontend.

GitHub documentation: https://docs.github.com/en/get-started/start-your-journey/deploying-your-website-automatically

## Import additional files from your Mac

First extract this package into a new project folder. Run the importer in dry-run mode; your original thesis folder is never modified:

```bash
python3 scripts/import_local_thesis.py --source "/Users/gustavotrainotticrispim/thesis-grid-ai"
```

Review the generated manifest, skipped files, and conflicts. After reviewing, run the same command with `--apply` to copy new files. Existing different files are never overwritten. Reconcile those conflicts manually. The importer never stages, commits, pushes, or changes Git remotes.

## Benchmark and scope

RTS-GMLC: https://github.com/GridMod/RTS-GMLC

Barrows et al., *The IEEE Reliability Test System: A Proposed 2019 Update*: https://doi.org/10.1109/TPWRS.2019.2925557

No capital costs, N−1 security, unit commitment, ramping, storage energy dynamics, or interday demand shifting are modeled. No final thesis manuscript or publication acceptance is represented. The opening animation is an illustrative schematic; the numerical explorers use saved results.
