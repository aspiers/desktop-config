// ==UserScript==
// @name       bugzilla page id helpers
// @namespace  https://github.com/aspiers/desktop-config/tree/master/lib/browser/userscripts/page-id-helpers
// @version    0.2.1
// @description Functions to return identifiers for bugzilla pages
// @match      https://bugzilla.kernel.org/*
// @match      https://bugzilla.suse.com/*
// @match      https://bugzilla.opensuse.org/*
// @match      https://bugzilla.novell.com/*
// @match      https://bugzilla.gnome.org/*
// @match      https://bugs.kde.org/*
// @match      https://bugzilla.redhat.com/*
// @author     Adam Spiers
// @grant      none
// @downloadURL https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/page-id-helpers/bugzilla%20page%20id%20helpers.user.js
// ==/UserScript==

var bugzilla_extract_title = function () {
    var m = document.title.match(/^Bug (\d+) – (.+)$/);
    if (m) {
        return({
            number: m[1],
            title: m[2],
        });
    }
    return null;
};

// https://en.opensuse.org/openSUSE:Packaging_Patches_guidelines#Current_set_of_abbreviations
var bugzilla_abbreviation = function () {
    switch (window.location.hostname) {
    case "bugzilla.kernel.org":
        return "bko";
    case "bugzilla.suse.com":
        return "bsc";
    case "bugzilla.opensuse.org":
        return "boo";
    case "bugzilla.novell.com":
        return "bnc";
    case "bugzilla.gnome.org":
        return "bgo";
    case "bugs.kde.org":
        return "kde";
    case "bugzilla.redhat.com":
        return "rh";
    default:
        return null;
    }
};

var page_id = function () {
    var m = bugzilla_extract_title();
    if (m) {
        var abbrev = bugzilla_abbreviation();
        if (abbrev) {
            return(abbrev + "#" + m.number);
        }
    }
    return null;
};

var page_title = function () {
    var m = bugzilla_extract_title();
    if (m) {
        var abbrev = bugzilla_abbreviation();
        if (abbrev) {
            return(abbrev + "#" + m.number + " (" + m.title + ")");
        }
    }
    return(document.title);
};
