// ==UserScript==
// @name           Always login on Micro Focus sites
// @description    Automatically fill out login form and submit it
// @include        /https?://(.*\.)?(suse|novell|attachmategroup|microfocus|opensuse)\.(com|net|org)/.*/
// @grant          none
// @downloadURL    https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/Always%20login%20on%20Micro%20Focus%20sites.user.js
// @version        0.5.0
// @author         Dirk Mueller, Adam Spiers, Bernhard M. Wiedemann, Oliver Kurz
// ==/UserScript==

function checkForSignIn() {
    var loc = location.href;

    // redirect to the green side of life
    if (loc.match('^https://bugzilla.novell.com')) {
        window.location.replace(loc.replace('https://bugzilla.novell.com', 'https://bugzilla.suse.com'));
        return;
    }

    // if not logged in, redirect to the login page
    if (loc.match('^https://bugzilla.(suse.com|opensuse.org)')) {
        if (loc.indexOf('?GoAheadAndLogin=') < 0 && document.getElementById('login_link_top')) {
            var new_url = loc;
            if (new_url.indexOf('?') < 0) {
                new_url = new_url.concat('?');
            }
            new_url = new_url.replace('?', '?GoAheadAndLogIn=1&');
            window.location.replace(new_url);
            return;
        }
    }

    // auto-submit login form when username and password are populated
    var form = document.forms[0];
    // gwmail has a element 'action' as well as an attribute so we can not use the attribute of form
    if (form && (form.attributes['action'].value.match('^(https://login.(microfocus|innerweb.novell).com|/gw/webacc)'))) {
        var uid = document.getElementById('username');
        var pw = document.getElementById('password');
        if (!(uid && pw)) {
            console.warn('login form missing username/password field?!');
            return;
        }
        if (!(uid.value && pw.value) || uid.value === '' || pw.value === '') {
            console.info('login form missing username/password; can\'t auto-login. ' +
                         'Will check again in 2s.');
            setTimeout(checkForSignIn, 2000);
            return;
        } // needed because submit button has name='submit' which overlays the form submit function

        document.createElement('form').submit.call(form);
    }
}
checkForSignIn();
