// ==UserScript==
// @name       OBS page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.1.6
// @description Functions to return identifiers for Open Build Service pages
// @match      https://build.opensuse.org/*
// @match      https://build.suse.de/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/OBS%20page%20id%20helpers.user.js
// ==/UserScript==

window.obs_extract_title = function () {
    var title = document.title;
    var obs, m;
    if (m = title.match(/(.+) - openSUSE Build Service$/)) {
        title = m[1];
        obs = "OBS";
    }
    else if (m = title.match(/(.+) - SUSE Internal OBS Instance$/)) {
        title = m[1];
        obs = "IBS";
    }
    else {
        return null;
    }

    var m = title.match(/^Show (.+)$/);
    if (m) {
        title = m[1];
    }

    return(obs + ": " + title);
};

window.page_id = window.obs_extract_title;
window.page_title = window.obs_extract_title;
