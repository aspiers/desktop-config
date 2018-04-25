// ==UserScript==
// @name           Auto-redirect from suse-jira.dyndns.org
// @description    Auto-redirect from suse-jira.dyndns.org to jira.prv.suse.net
// @match          https://suse-jira.dyndns.org/*
// @grant          none
// @downloadURL    https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/redirect%20JIRA%20to%20prv.user.js
// @version        0.1.0
// @author         Dirk Mueller, Adam Spiers, Bernhard M. Wiedemann, Oliver Kurz
// ==/UserScript==

function SUSEJIRARedirect() {
    var loc = location.href;

    // redirect to the green side of life
    if (loc.match('^https://suse-jira.dyndns.org')) {
        window.location.replace(
            loc.replace(
                'https://suse-jira.dyndns.org',
                'https://jira.prv.suse.net'
            )
        );
        return;
    }
}

SUSEJIRARedirect();
