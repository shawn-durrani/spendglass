# Acknowledgements

Spendglass itself is [MIT](LICENSE) licensed. One third-party component is
vendored into this repository and carries its own licence.

## Apache ECharts

`spendglass/static/echarts.min.js` is a copy of [Apache
ECharts](https://echarts.apache.org/) 6.0.0, licensed under the [Apache
License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).
Copyright belongs to the Apache Software Foundation and the ECharts
contributors.

The Apache licence header that ECharts distributes the bundle with is
retained inline at the top of the vendored file, so the notice travels
with the code. It is vendored rather than loaded from a CDN because a
page load in Spendglass must fetch nothing from the internet.

The bundle also carries an inline copyright and permission notice from
Microsoft Corporation, covering the TypeScript helper code compiled into
it. That notice is likewise retained inside the file.
