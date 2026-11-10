// Trusted Types policy (CSP: require-trusted-types-for 'script'; trusted-types khj). The only
// script URL this app ever needs is its own service worker.
interface TTPolicy {
  createScriptURL(url: string): unknown;
}
interface TTFactory {
  createPolicy(name: string, rules: { createScriptURL(url: string): string }): TTPolicy;
}

const factory = (globalThis as { trustedTypes?: TTFactory }).trustedTypes;
const policy = factory?.createPolicy("khj", {
  createScriptURL(url: string) {
    if (url === "/sw.js") return url;
    throw new TypeError("blocked script URL");
  },
});

export function serviceWorkerUrl(): string {
  return (policy ? policy.createScriptURL("/sw.js") : "/sw.js") as string;
}
