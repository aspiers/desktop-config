// ==UserScript==
// @name       default page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.4.3
// @description Functions to return default identifiers for all pages
// @match      *://*/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/00%20default%20page%20id%20helpers.user.js
// ==/UserScript==

// N.B. We have to make sure that the default implementations of
// page_id() and page_title() don't override implementations provided
// by site-specific helpers, since we have no guarantee of the order
// in which the helpers are all loaded by Greasemonkey / Tampermonkey.
if (window.page_id === undefined) {
    window.page_id = function () {
        alert(
            "No page_id() helper matched for " + location.href +
                " - check that you have the correct helpers installed " +
                "and that they are correctly matching the sites you expect."
        );
        return(null);
    };
}

if (window.page_title === undefined) {
    window.page_title = function () {
        return(document.title);
    };
}

// Quoting apostrophes is required due to an apparent bug in gvfs;
// reported in exo:
//
//   https://gitlab.xfce.org/xfce/exo/-/issues/6
//
// which was originally reported here:
//
//   https://bugzilla.xfce.org/show_bug.cgi?id=10410
//
// and links to
//
//   https://bugzilla.gnome.org/show_bug.cgi?id=683821
//
// However this isn't the only issue; it gets really weird with
// UTF characters:
//
//   https://gitlab.xfce.org/xfce/exo/-/issues/108
//
// So a hacky workaround is to edit xdg-open to pretend exo-open
// isn't installed; then it will use gio open which works flawlessly.
window.encodeURIComponent2 = function (s) {
  // This is generalised in case we need to quote anything else.
  return(encodeURIComponent(s).replace(/(['])/g, m => "%" + m.charCodeAt(0).toString(16)));
};

window.open_external_URL = function (url) {
    // This method seems somewhat unreliable:
    location.href = url;

    // This one is more reliable, but I can't get the new window to close,
    // even with @grant window.close:
    // var w = window.open(url);
    // w.onload = function () {
    //     w.close();
    // };
};

window.xclip = function (text) {
    window.open_external_URL("xclip://" + window.encodeURIComponent2(text));
};

// See emacs docstrings for org-protocol-store-link and
// org-protocol-capture
window.org_protocol = function (action, text, template) {
    var url = "org-protocol://" +
        action + "?" +
        "url=" + window.encodeURIComponent2(location.href) +
        "&title=" + window.encodeURIComponent2(text) +
        "&body=" + window.encodeURIComponent2(window.getSelection());
    if (template) {
        url += "&template=" + template;
    }
    window.open_external_URL(url);
};
