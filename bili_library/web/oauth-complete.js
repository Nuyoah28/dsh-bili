// Top-level navigation after the same-origin form completes; no credentials are
// read here. The server has already checked the callback against registration.
const destination = document.getElementById('oauth-return');
if (destination) window.location.replace(destination.href);
