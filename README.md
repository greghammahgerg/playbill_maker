# Seven Hills Artist Portal

## Routes

- `/` is the public artist directory.
- `/form` is the artist submission form. It requires a Firebase-authenticated Google account with a verified email address.
- `/admin` is the seasonal-lineup dashboard. It requires the same Google sign-in flow and an email listed in `ADMIN_EMAILS`.

## Authentication setup

Enable Google as a provider in Firebase Authentication, then configure these environment variables:

```text
FIREBASE_API_KEY=
FIREBASE_AUTH_DOMAIN=
FIREBASE_PROJECT_ID=
FIREBASE_APP_ID=
FIREBASE_CREDENTIALS_PATH=path/to/firebase-service-account.json
ADMIN_EMAILS=admin-one@example.com,admin-two@example.com
```

`FIREBASE_CREDENTIALS_PATH` must point to a Firebase service-account key for the same Firebase project as the web configuration. The server verifies each Firebase ID token before creating a session; a missing or unverified email cannot access or submit `/form`.
