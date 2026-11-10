// Copy the shared design tokens and self-hosted fonts into public/shared (served at /shared).
import { cpSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const target = join(root, "public", "shared");
mkdirSync(join(target, "fonts"), { recursive: true });
cpSync(join(root, "..", "shared", "tokens.css"), join(target, "tokens.css"));
cpSync(join(root, "..", "shared", "fonts"), join(target, "fonts"), { recursive: true });
