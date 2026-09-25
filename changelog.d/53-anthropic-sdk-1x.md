- Merchant identification now runs on version 1 of Anthropic's Python
  library (#53). The requirement used to accept anything from 0.120 up,
  so a fresh install got 1.x while older installs stayed on 0.x. It now
  asks for 1.8.0 or later and stops below 2. An existing install
  upgrades on its next start, and the miners work as before. A new test
  sends every miner request through the real library to a fake API, so
  a release that drops something spendglass sends fails the suite.
