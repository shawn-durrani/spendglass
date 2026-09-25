- Your other apps are one click away (workbench#100). A row at the top
  of both pages links Crossband, Membro and Threadfold. Spendglass asks
  each one on this machine where a browser can open it, keeps the
  answers for a minute, and leaves out any app that doesn't answer.
  `/api/session` now also answers `app` and `browser_origin`, so the
  other apps can link here. That address is the local one, because
  Spendglass answers only on this machine, so their rows show it on the
  Mac and leave it out on a phone. A new `SPENDGLASS_SIBLING_APPS`
  setting names the apps.
