- Removing a passkey now signs out every other browser (#69), the same
  rule crossband and membro follow. The browser doing the removal stays
  signed in with a fresh session. An expired session is also cleared on
  the next sign-in, where before it waited for a restart.
