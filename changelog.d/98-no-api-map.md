- Spendglass no longer serves a map of its web API at `/openapi.json`
  (#98). Anyone who could reach the app could read it without signing
  in, and it listed every route, the admin ones included. Nothing used
  it, and the interactive pages built on it were already off.
