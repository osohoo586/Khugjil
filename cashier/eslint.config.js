import js from "@eslint/js";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist/**", "public/**", "node_modules/**"] },
  js.configs.recommended,
  ...tseslint.configs.strict,
  {
    rules: {
      // Payer-written descriptions are untrusted: text only, never HTML (SPEC §3.6).
      "no-restricted-properties": [
        "error",
        { property: "innerHTML", message: "Use textContent / dom.h()." },
        { property: "outerHTML", message: "Use textContent / dom.h()." },
        { property: "insertAdjacentHTML", message: "Use dom.h()." },
        { object: "document", property: "write", message: "Never." },
      ],
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
    },
  },
);
