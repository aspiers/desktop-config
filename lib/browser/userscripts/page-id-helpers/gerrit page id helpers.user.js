// ==UserScript==
// @name       gerrit page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.2.0
// @description Functions to return identifiers for gerrit pages
// @match      https://review.openstack.org/*
// @match      https://gerrit.suse.provo.cloud/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/gerrit%20page%20id%20helpers.user.js
// ==/UserScript==

gerrit_extract_title = function () {
    var m = document.title.match(/^Change (I[0-9a-f]+): (.+) \| .* Code Review$/);
    if (m) {
        return({
            id: m[1],
            title: m[2],
        });
    }
    return null;
};

page_id = function () {
    var m = gerrit_extract_title();
    if (m) {
        return("gerrit#" + m.id);
    }
    return(null);
};

page_title = function () {
    var m = gerrit_extract_title();
    if (m) {
        return("gerrit#" + m.id + " (" + m.title + ")");
    }
    return(document.title);
};
