// The Mac link's lines on the tablet page (docs/voice-desk.md, "What the
// desk page shows"): one line per listed host, with its host badge, and the
// unattributed refusal lines, never under a host. Renders only; the words
// are desk_hosts.js's. Text only ever goes through textContent.
(function (root) {
  "use strict";

  var Hosts = root.DeskHosts || require("./desk_hosts.js");

  // doc: the page's document.
  function create(doc) {
    var linkList = doc.getElementById("links");
    var refusalList = doc.getElementById("link-refusals");

    function el(tag, props) {
      var node = doc.createElement(tag);
      Object.keys(props).forEach(function (k) {
        if (k === "text") node.textContent = props[k];
        else if (k === "class") node.className = props[k];
        else node.setAttribute(k, props[k]);
      });
      return node;
    }

    function empty(node) { while (node.firstChild) node.removeChild(node.firstChild); }

    // A host badge, as the targets show it.
    function hostBadge(host) {
      return el("span", { "class": "badge host", "data-testid": "host-badge", text: Hosts.hostLabel(host) });
    }

    // state: the snapshot, or null when unpaired (nothing the hub sent stays).
    function render(state) {
      empty(linkList);
      empty(refusalList);
      var listed = state && Array.isArray(state.links) ? state.links : [];
      listed.forEach(function (link) {
        var line = Hosts.linkLine(link);
        var li = el("li", { "data-testid": "link-line", "data-host": link.host });
        li.appendChild(hostBadge(link.host));
        li.appendChild(el("span", { "data-tone": line.tone, text: line.text }));
        linkList.appendChild(li);
      });
      Hosts.refusalLines(state).forEach(function (text) {
        refusalList.appendChild(el("li", { "data-testid": "link-refusal", "data-tone": "warn", text: text }));
      });
      linkList.hidden = !listed.length;
      refusalList.hidden = !refusalList.firstChild;
    }

    return { render: render, hostBadge: hostBadge };
  }

  root.DeskLinkView = { create: create };
})(typeof window !== "undefined" ? window : globalThis);
