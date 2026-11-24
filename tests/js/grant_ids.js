// Grant ids in the hub's shape (grants.py: GRANT_ID_BYTES random bytes in
// lowercase hex), one per name, for the controller and page harnesses and
// their tests.
"use strict";
const G = Object.freeze({
  g1: "1".repeat(32), g2: "2".repeat(32), late: "3".repeat(32), old: "4".repeat(32), slow: "5".repeat(32)
});
module.exports = { G };
