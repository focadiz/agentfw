# Platform: iOS (Swift / SwiftUI)

## Project conventions

- SwiftUI for all new screens. One view per file. Keep views free of networking and business logic.
- MVVM: each screen gets a view model marked `@MainActor` and `@Observable` (or `ObservableObject` if the project targets older iOS). Views read state from it and call its methods.
- Networking lives in a small API client, one method per endpoint, using `async`/`await` and `URLSession`. No third-party networking libraries unless the project already uses one.
- Request and response types are `Codable` structs that mirror the API contract (`api-contract` knowledge source) field for field. Check field names and optionality against the RAML; don't guess.
- Base URLs and environment settings come from configuration, never hardcoded. Never put API keys or tokens in source.

## Code rules

- No force unwraps (`!`) or `try!` outside tests.
- Every network call handles errors and maps them to a user-facing message. Where the project docs specify the exact message text (e.g. edge conditions), use it verbatim.
- Client-side validation (email format, phone number) happens before the request, and the server's validation errors are still handled.
- Strings shown to users go through `String(localized:)`.
- Accessibility: every interactive element has an accessibility label.

## Tests

- Unit-test view models and the API client with a stubbed `URLProtocol` or a protocol-based client. No real network calls in tests.
- Test the error paths the acceptance criteria mention, not just the happy path.
- Name tests after the behavior: `test_signUp_whenEmailAlreadyRegistered_showsLoginPrompt`.

## Don't touch

- Signing, provisioning profiles, entitlements, `.xcconfig` files and team settings. They're blocked anyway.
- Don't add files to the project by editing `project.pbxproj` by hand. If a new file must be added to a target and the project doesn't use folder references or Swift packages, say so in `notes_for_reviewer` and ask the human to add it.
