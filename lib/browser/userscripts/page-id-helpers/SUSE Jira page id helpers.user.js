// ==UserScript==
// @name       Jira page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.0.1
// @description Functions to return identifiers for SUSE Jira pages
// @match      https://suse-jira.dyndns.org/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/SUSE%20Jira%20page%20id%20helpers.user.js
// ==/UserScript==

jira_extract_metadata = function () {
    var m = document.title.match(/^\[([^\]]+)\] (.+) - SUSE Jira$/);
    if (m) {
        return({
            id: m[1],
            title: m[2],
        });
    }
    return null;
};

page_id = function () {
    var m = jira_extract_metadata();
    if (m) {
        return(m.id);
    }
    return(null);
};

page_title = function () {
    var m = jira_extract_metadata();
    if (m) {
        return("[" + m.id + "] " + m.title);
    }
    return(document.title);
};
