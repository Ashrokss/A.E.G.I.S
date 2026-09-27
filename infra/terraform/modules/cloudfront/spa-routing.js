// Viewer-request on the S3 behaviour: any path without a file extension is a
// client-side route, so serve the SPA shell.
function handler(event) {
  var req = event.request;
  if (!/\.[a-zA-Z0-9]+$/.test(req.uri)) {
    req.uri = "/index.html";
  }
  return req;
}
