// ==UserScript==
// @name           Always login on launchpad.net
// @description    Automatically authorize authentication via launchpad
// @include        https://login.launchpad.net/*/+decide
// @include        https://login.ubuntu.com/*/+decide
// @downloadURL    https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/Always%20login%20on%20launchpad.net.user.js
// @grant          none
// @version        0.2.0
// @author         Adam Spiers
// ==/UserScript==

var form = document.querySelector('div#auth form');

if (form && form.action.match("^https://login\.(launchpad\.net|ubuntu\.com)/")) {
    form.submit();
}
