- A wrong password no longer freezes the app for everyone (#97). After a
  failed sign-in Spendglass still holds its answer for up to 3 seconds,
  to slow down guessing, but the rest of the app keeps answering while
  it waits, including your other tabs and the deploy watcher's busy
  check.
