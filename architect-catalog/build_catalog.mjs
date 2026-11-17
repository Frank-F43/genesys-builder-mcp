#!/usr/bin/env node
// Builds a catalog of every Architect action and menu: which flow types offer
// it in this org, and which properties it has. Run through ./refresh.sh, which
// installs the current Architect Scripting SDK first.
//
// Availability comes live from the org through the SDK, so it reflects the
// SDK release and the org's licensed products. Properties come from the SDK's
// own type definitions. Archy's YAML is that object model written out: the
// YAML key is the class name without "ArchAction"/"ArchMenu", lower-camel-cased
// (ArchActionTransferToAcd -> transferToAcd), and the property names match.

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SDK_NAME = "purecloud-flow-scripting-api-sdk-javascript";
const SDK_DIR = path.join(HERE, "node_modules", SDK_NAME);
const OUTPUT = path.join(HERE, "..", "reference", "architect", "catalog.json");

const SDK_LOCATIONS = {
  "apne2.pure.cloud": "prod_ap_northeast_2",
  "apne3.pure.cloud": "prod_ap_northeast_3",
  "aps1.pure.cloud": "prod_ap_south_1",
  "apse1.pure.cloud": "prod_ap_southeast_1",
  "cac1.pure.cloud": "prod_ca_central_1",
  "euc2.pure.cloud": "prod_eu_central_2",
  "edee1.eusc-pure.cloud": "prod_eusc_de_east_1",
  "euw2.pure.cloud": "prod_eu_west_2",
  "mec1.pure.cloud": "prod_me_central_1",
  "mxc1.pure.cloud": "prod_mx_central_1",
  "mypurecloud.com": "prod_us_east_1",
  "mypurecloud.com.au": "prod_ap_southeast_2",
  "mypurecloud.de": "prod_eu_central_1",
  "mypurecloud.ie": "prod_eu_west_1",
  "mypurecloud.jp": "prod_ap_northeast_1",
  "sae1.pure.cloud": "prod_sa_east_1",
  "use2.us-gov-pure.cloud": "prod_us_east_2",
  "usw2.pure.cloud": "prod_us_west_2",
};

const ENV_KEYS = ["GENESYSCLOUD_REGION", "GENESYSCLOUD_OAUTHCLIENT_ID", "GENESYSCLOUD_OAUTHCLIENT_SECRET"];

// Same resolution as toolkit/scripts/genesys_config.py: the genesys-builder
// env block of the nearest .cursor/mcp.json, and the environment only where
// no such file exists.
function findMcpConfig() {
  for (const start of [process.cwd(), HERE]) {
    let dir = path.resolve(start);
    while (true) {
      const candidate = path.join(dir, ".cursor", "mcp.json");
      if (fs.existsSync(candidate)) return candidate;
      const parent = path.dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
  }
  return null;
}

function loadCredentials() {
  const file = findMcpConfig();
  const source = file
    ? JSON.parse(fs.readFileSync(file, "utf8"))?.mcpServers?.["genesys-builder"]?.env ?? {}
    : process.env;
  const values = ENV_KEYS.map((key) => source[key]);
  if (values.every(Boolean)) return values;
  throw new Error(`No credentials: set ${ENV_KEYS.join(", ")} in the genesys-builder env block of ${file ?? ".cursor/mcp.json"}`);
}

// Properties a class declares, with the first sentence of their doc comment.
// Inherited ones are resolved through the extends chain by the caller.
function parseTypes(text) {
  const classes = {};
  const classRe = /^export (?:abstract )?class (\w+)(?: extends (\w+))? \{\n([\s\S]*?)^\}/gm;
  for (const [, name, base, body] of text.matchAll(classRe)) {
    const properties = {};
    const memberRe = /(?:\/\*\*([\s\S]*?)\*\/\s*)?^\s{4}(readonly )?(\w+)\??: ([^;\n]+);/gm;
    for (const [, doc, readonly, prop, type] of body.matchAll(memberRe)) {
      if (prop === "displayTypeName" || /^isArch[A-Z]/.test(prop)) continue;
      const summary = (doc || "")
        .split("\n")
        .map((line) => line.replace(/^\s*\*\s?/, "").trim())
        .filter((line) => line && !line.startsWith("@"))
        .join(" ")
        .replace(/\{@link [^}]*?(\w+)\}/g, "$1")
        .split(/(?<=\.)\s/)[0];
      properties[prop] = { type: type.trim(), readonly: Boolean(readonly), doc: summary };
    }
    classes[name] = { base, properties };
  }
  return classes;
}

function flatten(classes, name, stopAt) {
  const merged = {};
  const chain = [];
  for (let current = name; current && current !== stopAt && classes[current]; current = classes[current].base) {
    chain.unshift(current);
  }
  for (const cls of chain) Object.assign(merged, classes[cls].properties);
  return merged;
}

const lowerFirst = (s) => s.charAt(0).toLowerCase() + s.slice(1);

function redactConsole(secret) {
  const scrub = (value) =>
    typeof value === "string"
      ? value
          .split(secret)
          .join("***")
          .replace(/(clientSecret: ')[^']*/g, "$1***")
          .replace(/(auth token ')[^']*/g, "$1***")
          .replace(/(Bearer )[\w.-]+/g, "$1***")
      : value;
  for (const method of ["log", "info", "warn", "error", "debug"]) {
    const original = console[method].bind(console);
    console[method] = (...args) => original(...args.map(scrub));
  }
}

async function main() {
  if (!fs.existsSync(SDK_DIR)) throw new Error(`SDK not installed in ${SDK_DIR} - run ./refresh.sh`);
  const sdkVersion = JSON.parse(fs.readFileSync(path.join(SDK_DIR, "package.json"), "utf8")).version;
  const types = parseTypes(fs.readFileSync(path.join(SDK_DIR, "types.d.ts"), "utf8"));

  const [region, clientId, clientSecret] = loadCredentials();
  const location = SDK_LOCATIONS[region];
  if (!location) throw new Error(`No Architect Scripting location known for region ${region}`);

  // The SDK's notes print the client secret and the access token verbatim.
  // Notes stay off, and anything else it prints is redacted on the way out.
  redactConsole(clientSecret);

  const require = createRequire(import.meta.url);
  const scripting = require(SDK_NAME);
  const session = scripting.environment.archSession;
  const logging = scripting.services.archLogging;
  logging.logNotes = false;
  logging.logNotesVerbose = false;
  logging.logNetworkActivity = false;
  logging.logWarnings = false;
  session.endTerminatesProcess = false;

  let catalog;
  await session.startWithClientIdAndSecret(
    location,
    async () => {
      const flowTypes = scripting.enums.archEnums.FLOW_TYPES_ALL;
      const describe = (namespace, prefix, stopAt) => {
        const entries = {};
        for (const [className, cls] of Object.entries(namespace)) {
          if (!className.startsWith(prefix) || className === prefix) continue;
          let definition;
          try {
            definition = cls.definition;
          } catch {
            continue;
          }
          if (!definition) continue;
          entries[lowerFirst(className.slice(prefix.length))] = {
            class: className,
            label: cls.label ?? definition.label ?? null,
            flowTypes: [...(definition.availableFlowTypes ?? [])].sort(),
            canCreate: Boolean(definition.canCreateFromDefinition),
            extends: types[className]?.base ?? null,
            properties: flatten(types, className, stopAt),
          };
        }
        return entries;
      };

      // Stopping below ArchBaseAction / ArchBaseMenu drops what every action
      // or menu shares (name, trackingId, parent links) and keeps what is
      // specific to it.
      const actions = describe(scripting.viewModels.actions, "ArchAction", "ArchBaseAction");
      const menus = describe(scripting.viewModels.menus, "ArchMenu", "ArchBaseMenu");

      // A flow type with no actions is usually one the org is not licensed or
      // enabled for; isAvailable says so explicitly.
      const flowDefinitions = {};
      for (const [className, cls] of Object.entries(scripting.viewModels.flows)) {
        let definition;
        try {
          definition = cls.definition;
        } catch {
          continue;
        }
        if (definition?.type) flowDefinitions[definition.type] = { className, definition };
      }

      const byFlowType = {};
      for (const flowType of flowTypes) {
        const found = flowDefinitions[flowType];
        byFlowType[flowType] = {
          class: found?.className ?? null,
          label: found?.definition.label ?? null,
          available: found ? Boolean(found.definition.isAvailable) : null,
          creatable: found ? Boolean(found.definition.isCreatable) : null,
          deprecated: found ? Boolean(found.definition.isDeprecated) : null,
          actions: Object.keys(actions).filter((key) => actions[key].flowTypes.includes(flowType)),
          menus: Object.keys(menus).filter((key) => menus[key].flowTypes.includes(flowType)),
        };
      }

      catalog = {
        generatedAt: new Date().toISOString(),
        sdk: { name: SDK_NAME, version: sdkVersion },
        org: { region, name: session.orgInfo?.name ?? null, id: session.orgInfo?.id ?? null },
        flowTypes: byFlowType,
        actions,
        menus,
      };
    },
    clientId,
    clientSecret,
    undefined,
    true,
  );

  if (!catalog) throw new Error("The Architect Scripting session ended before the catalog was built");
  fs.mkdirSync(path.dirname(OUTPUT), { recursive: true });
  fs.writeFileSync(OUTPUT, JSON.stringify(catalog, null, 2) + "\n");

  const counts = Object.entries(catalog.flowTypes)
    .map(([type, entry]) => `${type} ${entry.available === false ? "n/a" : entry.actions.length}`)
    .join(", ");
  console.log(`SDK ${sdkVersion}, org ${catalog.org.name ?? region}`);
  console.log(`${Object.keys(catalog.actions).length} actions, ${Object.keys(catalog.menus).length} menus`);
  console.log(`actions per flow type: ${counts}`);
  console.log(`written to ${OUTPUT}`);
}

// The ended session still holds sockets open, so the process would never exit.
main().then(() => process.exit(0)).catch((error) => {
  console.error(error?.message ?? error);
  process.exit(1);
});
