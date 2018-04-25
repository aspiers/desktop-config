// ==UserScript==
// @name           Auto-redirect from bnc to bsc
// @description    Auto-redirect from bugzilla.novell.com to bugzilla.suse.com
// @match          https://bugzilla.novell.com/*
// @grant          none
// @downloadURL    https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/redirect%20to%20bsc.user.js
// @version        0.1.0
// @author         Dirk Mueller, Adam Spiers, Bernhard M. Wiedemann, Oliver Kurz
// ==/UserScript==

function bscRedirect() {
    var loc = location.href;

    // redirect to the green side of life
    if (loc.match('^https://bugzilla.novell.com')) {
        window.location.replace(
            loc.replace(
                'https://bugzilla.novell.com',
                'https://bugzilla.suse.com'
            )
        );
        return;
    }
}

bscRedirect();
