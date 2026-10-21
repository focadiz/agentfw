# Platform: MuleSoft deployments (CloudHub via GitHub Actions)

## How RideXpress deploys

- `.github/workflows/${pack.deploy_workflow}` runs on every push to `main`.
- It diffs the merge commit against its parent and deploys each changed top-level folder that is a Mule app. `.github`, `util`, `docs`, `pom.xml` and `README.md` are skipped.
- So: merge = deploy, and one PR touching two apps deploys two apps.

## Reading failures

Check these first; they cover most failed runs:
- **Maven settings / auth:** 401 or 403 from Exchange or the MuleSoft repo means a secret or the generated `settings.xml` is wrong. That's a human fix, since you can't see secrets.
- **Dependency resolution:** a missing artifact usually means a version range or Exchange dependency that isn't published.
- **Deployment timeouts:** the app started but failed health checks. Look for the app's startup error in the logs.
- **Nothing deployed:** if the diff only touched skipped folders, a green run deploys nothing. Say so in the report rather than marking success.

## Health checks

Run the `health` check once per deployed app, with `app` set to the app folder name. A failing health check after a green run is a failed deployment.
