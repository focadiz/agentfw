# Platform: MuleSoft (Mule 4)

## Architecture

- Follow API-led layering. Experience APIs shape data for a client and call process APIs. Process APIs orchestrate and hold business rules. System APIs wrap exactly one backend system. A layer never skips the one below it.
- The RAML spec in `src/main/resources/api/` is the contract. Implement it exactly. Flows are generated from it by APIkit, so keep APIkit flow names (`post:\users:application\json:...-config`) unchanged.
- Shared types, traits and security schemes come from Exchange modules. Reuse them; don't redefine types locally.

## Flow conventions

- Keep APIkit flows thin: each calls one `flow-ref` to an implementation flow in a separate file named after the resource (e.g. `users.xml`).
- All transformation in DataWeave 2. Put non-trivial scripts in `src/main/resources/dwl/` and reference them, so they can be unit-tested.
- Config values come from properties files per environment (`${env}`), with secrets in secure properties (`${secure::...}`). Never put credentials in XML or plain properties.
- Propagate the `transactionId` header on every outbound call (the project's `transactionId` trait).

## Errors and retries

- Use the global error handler pattern: map connector errors to API errors with the right HTTP status and the project's error body format.
- Where the docs specify retries (e.g. "retry up to 3 times with exponential backoff"), implement them with `until-successful` or a retry flow, with the exact counts and delays from the docs.
- When a multi-step process fails partway and the docs require rollback (e.g. "no partial user record"), implement the compensation step explicitly and test it.

## Tests (MUnit)

- One MUnit suite per implementation file. Mock every outbound HTTP call and connector; no real external calls.
- Cover the happy path and every error path in the task's acceptance criteria. Assert on HTTP status and response body.
- Keep coverage above the project's threshold if `pom.xml` sets one.

## Don't touch

- `pom.xml` parent, plugin versions and the deploy configuration, unless the task explicitly says so.
- Secure properties files.
