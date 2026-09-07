"use strict";

/*!
 * baguetteBox.js
 * @author  feimosi
 * @version 1.11.1-mod
 * @url https://github.com/feimosi/baguetteBox.js
 */

var J_BBX = 1;

window.baguetteBox = (function () {
    'use strict';

    var options = {},
        defaults = {
            captions: true,
            buttons: 'auto',
            noScrollbars: false,
            bodyClass: 'bbox-open',
            titleTag: false,
            async: false,
            preload: 2,
            refocus: true,
            afterShow: null,
            afterHide: null,
            duringHide: null,
            onChange: null,
            readDirRtl: false,
        },
        overlay, slider, btnPrev, btnNext, btnHelp, btnAnim, btnRotL, btnRotR, btnSel, btnFull, btnZoom, btnVmode, btnVcode, btnReadDir, btnGoPage, btnClose,
        currentGallery = [],
        currentIndex = 0,
        isOverlayVisible = false,
        touch = {},  // start-pos
        touchFlag = false,  // busy
        scrollCSS = ['', ''],
        scrollTimer = 0,
        re_i = APPLE ?
            /^[^?]+\.(a?png|avif|bmp|gif|hei[cf]s?|jfif|jpe?g|jxl|svg|tiff?|webp)(\?|$)/i :
            /^[^?]+\.(a?png|avif|bmp|gif|jfif|jpe?g|jxl|svg|tiff?|webp)(\?|$)/i,
        // which files open as a <video>; when the server can transcode we also
        // accept every container it will transcode (window.vcode_exts = the
        // server's --th-r-ffv set, published next to have_vcode; both arrive
        // via util.js's CGV1 import, which loads before us), since HLS is the
        // fallback for anything the browser cannot play by itself
        vc_exts = window.have_vcode ? (window.vcode_exts || []) : [],
        vc_re = function (exts, pfx) {
            // regex for a url whose extension is one of exts; never matches
            // when exts is empty (so a missing server list disables a rule)
            var ok = [];
            for (var a = 0; a < exts.length; a++)
                if (/^[a-z0-9]+$/i.test(exts[a]))
                    ok.push(exts[a]);
            return ok.length ? new RegExp(pfx + '\\.(' + ok.join('|') + ')(\\?|$)', 'i') : /(?!)/;
        },
        vc_has = function (exts) {
            // the subset of exts that the server will actually transcode; the
            // rules below must never send the server a container it refuses
            var ret = [];
            for (var a = 0; a < exts.length; a++)
                if (has(vc_exts, exts[a]))
                    ret.push(exts[a]);
            return ret;
        },
        re_v = vc_re(['webm', 'mkv', 'mp4', 'm4v', 'mov'].concat(vc_exts), '^[^?]+'),
        // legacy containers no browser can demux natively; transcode these
        // upfront instead of waiting for a native failure; modern/ambiguous
        // containers (mkv, ts, mp4, mpg, 3gp, ...) are deliberately absent; we
        // try those natively first and fall back only on an actual failure, so
        // we never transcode something the browser could have played
        re_vhls = vc_re(vc_has(['avi', 'wmv', 'flv', 'vob', 'ogm', 'rm', 'rmvb', 'divx', 'asf']), ''),
        // containers we play natively but where chrome can fail to decode the
        // video track silently (no 'error' event); for these, a loadedmetadata
        // with videoWidth==0 means the codec is unsupported, so transcode;
        // mp4/mov/webm/m4v are excluded since videoWidth==0 there is usually a
        // legit audio-only file that plays fine as-is
        re_vck = vc_re(vc_has(['mkv', 'ts', 'm2ts', 'mts', 'mpg', 'mpeg', '3gp']), ''),
        re_cbz = /^[^?]+\.(cbz)(\?|$)/i,
        anims = ['slideIn', 'fadeIn', 'none'],
        data = {},  // all galleries
        imagesElements = [],
        documentLastFocus = null,
        isFullscreen = false,
        isCbz = false,
        vmute = false,
        vloop = sread('vmode') == 'L',
        vnext = sread('vmode') == 'C',
        // manual video-source override: a=auto, t=force transcode, n=force native
        vsrc = sread('vsrc') || 'a',
        loopA = null,
        loopB = null,
        url_ts = null,
        un_pp = 0,
        resume_mp = false;

    var onFSC = function (e) {
        isFullscreen = !!document.fullscreenElement;
        clmod(document.documentElement, 'bb_fsc', isFullscreen);
    };

    var overlayClickHandler = function (e) {
        if (e.target.id.indexOf('baguette-img') !== -1)
            hideOverlay();
    };

    var vtouch = function (e) {
        var v = vid(),
            bv = v.getBoundingClientRect(),
            tp = e.changedTouches[0];

        if (bv.bottom - tp.clientY < 90)
            touchFlag = true;
    };

    var touchstartHandler = function (e) {
        touch.count = e.touches.length;
        if (touch.count > 1)
            touch.multitouch = true;

        touch.startX = e.changedTouches[0].pageX;
        touch.startY = e.changedTouches[0].pageY;
    };
    var touchmoveHandler = function (e) {
        if (touchFlag || touch.multitouch)
            return;

        e.preventDefault ? e.preventDefault() : e.returnValue = false;
        var touchEvent = e.touches[0] || e.changedTouches[0];
        if (touchEvent.pageX - touch.startX > 40) {
            touchFlag = true;
            showLeftImage();
        } else if (touchEvent.pageX - touch.startX < -40) {
            touchFlag = true;
            showRightImage();
        } else if (touch.startY - touchEvent.pageY > 100) {
            hideOverlay();
        }
    };
    var touchendHandler = function (e) {
        touch.count--;
        if (e && e.touches)
            touch.count = e.touches.length;

        if (touch.count <= 0)
            touch.multitouch = false;

        touchFlag = false;
    };
    var contextmenuHandler = function () {
        touchendHandler();
    };

    var overlayWheelHandler = function (e) {
        if (!options.noScrollbars || anymod(e))
            return;

        ev(e);

        var x = e.deltaX,
            y = e.deltaY,
            d = Math.abs(x) > Math.abs(y) ? x : y;

        if (e.deltaMode)
            d *= 10;

        if (Date.now() - scrollTimer < (Math.abs(d) > 20 ? 100 : 300))
            return;

        scrollTimer = Date.now();

        if (d > 0)
            showNextImageIgnoreReadDir();
        else
            showPreviousImageIgnoreReadDir();
    };

    var trapFocusInsideOverlay = function (e) {
        if (modal.busy)
            return;

        if (overlay.style.display === 'block' && (overlay.contains && !overlay.contains(e.target))) {
            e.stopPropagation();
            btnClose.focus();
        }
    };

    function run(selector, userOptions) {
        buildOverlay();
        removeFromCache(selector);
        return bindImageClickListeners(selector, userOptions);
    }

    function bindImageClickListeners(selector, userOptions) {
        var galleryNodeList = QSA(selector);
        var selectorData = {
            galleries: [],
            nodeList: galleryNodeList
        };
        data[selector] = selectorData;

        [].forEach.call(galleryNodeList, function (galleryElement) {
            var tagsNodeList = [];
            if (galleryElement.tagName === 'A')
                tagsNodeList = [galleryElement];
            else
                tagsNodeList = galleryElement.getElementsByTagName('a');
            if (have_zls)
                bindCbzClickListeners(tagsNodeList, userOptions);

            tagsNodeList = [].filter.call(tagsNodeList, function (element) {
                if (element.className.indexOf(userOptions && userOptions.ignoreClass) === -1)
                    return re_i.test(element.href) || re_v.test(element.href);
            });
            if (!tagsNodeList.length)
                return;

            var gallery = [];
            [].forEach.call(tagsNodeList, function (imageElement, imageIndex) {
                var imageElementClickHandler = function (e) {
                    if (ctrl(e) || e && e.shiftKey)
                        return true;

                    e.preventDefault ? e.preventDefault() : e.returnValue = false;
                    prepareOverlay(gallery, userOptions);
                    showOverlay(imageIndex);
                };
                var imageItem = {
                    eventHandler: imageElementClickHandler,
                    imageElement: imageElement,
                };
                bind(imageElement, 'click', imageElementClickHandler);
                gallery.push(imageItem);
            });
            selectorData.galleries.push(gallery);
        });

        return [selectorData.galleries, options];
    }

    function getCbzPage() {
        try {
            var ret = parseInt(/,p([0-9]+)(,|$)/.exec(location.hash)[1]);
            if (isNum(ret))
                return Math.max(Math.min(ret, currentGallery.length), 1) - 1;
        }
        catch (ex) { }
        return 0;
    }

    function bindCbzClickListeners(tagsNodeList, userOptions) {
        var cbzNodes = [].filter.call(tagsNodeList, function (element) {
            return re_cbz.test(element.href);
        });
        if (!tagsNodeList.length) {
            return;
        }

        [].forEach.call(cbzNodes, function (cbzElement, index) {
            var gallery = [];
            var eventHandler = function (e) {
                if (ctrl(e) || e && e.shiftKey)
                    return true;

                e.preventDefault ? e.preventDefault() : e.returnValue = false;
                fillCbzGallery(gallery, cbzElement, eventHandler).then(function () {
                    prepareOverlay(gallery, userOptions, true);
                    showOverlay(getCbzPage());
                }).catch(function (reason) {
                    console.error("cbz-ded", reason);
                    var t;
                    try {
                        t = uricom_dec(cbzElement.href.split('/').pop());
                    } catch (ex) { }

                    var msg = "Could not browse " + (t ? t : 'archive');
                    try {
                        msg += "\n\n" + reason.message;
                    } catch (ex) { }
                    toast.err(20, msg, 'cbz-ded');
                });
            }

            bind(cbzElement, "click", eventHandler);
        })
    }

    function fillCbzGallery(gallery, cbzElement, eventHandler) {
        if (gallery.length !== 0) {
            return Promise.resolve();
        }
        var href = cbzElement.href;
        var zlsHref = href + (href.indexOf("?") === -1 ? "?" : "&") + "zls";
        return fetch(zlsHref)
            .then(function (response) {
                if (response.ok) {
                    return response.json();
                } else {
                    throw new Error("Archive is invalid");
                }
            })
            .then(function (fileList) {
                var imagesList = fileList.map(function (file) {
                    return file["fn"];
                }).filter(function (file) {
                    return re_i.test(file);
                }).sort();

                if (imagesList.length === 0) {
                    throw new Error("Archive does not contain any images");
                }

                imagesList.forEach(function (imageName, index) {
                    var imageHref = href
                        + (href.indexOf("?") === -1 ? "?" : "&")
                        + "zget="
                        + encodeURIComponent(imageName);

                    var galleryItem = {
                        href: imageHref,
                        imageElement: cbzElement,
                        eventHandler: eventHandler,
                        page: index + 1,
                    };
                    gallery.push(galleryItem);
                });
            });
    }

    function clearCachedData() {
        for (var selector in data)
            if (data.hasOwnProperty(selector))
                removeFromCache(selector);
    }

    function removeFromCache(selector) {
        if (!data.hasOwnProperty(selector))
            return;

        var galleries = data[selector].galleries;
        [].forEach.call(galleries, function (gallery) {
            [].forEach.call(gallery, function (imageItem) {
                unbind(imageItem.imageElement, 'click', imageItem.eventHandler);
            });

            if (currentGallery === gallery)
                currentGallery = [];
        });

        delete data[selector];
    }

    function buildOverlay() {
        overlay = ebi('bbox-overlay');
        if (!overlay) {
            var ctr = mknod('div');
            ctr.innerHTML = (
                '<div id="bbox-overlay" role="dialog">' +
                '<div id="bbox-slider"></div>' +
                '<button id="bbox-prev" class="bbox-btn" type="button" aria-label="Previous">&lt;</button>' +
                '<button id="bbox-next" class="bbox-btn" type="button" aria-label="Next">&gt;</button>' +
                '<div id="bbox-btns">' +
                '<button id="bbox-help" type="button">?</button>' +
                '<button id="bbox-anim" type="button" tt="a">-</button>' +
                '<button id="bbox-readdir" type="button" tt="a">ltr</button>' +
                '<button id="bbox-rotl" type="button">↶</button>' +
                '<button id="bbox-rotr" type="button">↷</button>' +
                '<button id="bbox-tsel" type="button">sel</button>' +
                '<button id="bbox-full" type="button" tt="full-screen">⛶</button>' +
                '<button id="bbzoom" type="button" tt="zoom/stretch">z</button>' +
                '<button id="bbox-vmode" type="button" tt="a"></button>' +
                '<button id="bbox-vcode" type="button" tt="a"></button>' +
                '<button id="bbox-gopage" type="button" tt="go to page">pg</button>' +
                '<button id="bbox-close" type="button" aria-label="Close">X</button>' +
                '</div></div>'
            );
            overlay = ctr.firstChild;
            QS('body').appendChild(overlay);
            tt.att(overlay);
        }
        slider = ebi('bbox-slider');
        btnPrev = ebi('bbox-prev');
        btnNext = ebi('bbox-next');
        btnHelp = ebi('bbox-help');
        btnAnim = ebi('bbox-anim');
        btnReadDir = ebi('bbox-readdir');
        btnRotL = ebi('bbox-rotl');
        btnRotR = ebi('bbox-rotr');
        btnSel = ebi('bbox-tsel');
        btnFull = ebi('bbox-full');
        btnZoom = ebi('bbzoom');
        btnVmode = ebi('bbox-vmode');
        btnVcode = ebi('bbox-vcode');
        btnGoPage = ebi('bbox-gopage');
        btnClose = ebi('bbox-close');

        bcfg_bind(options, 'bbzoom', 'bbzoom', false, setzoom);
        setzoom();
    }

    function halp() {
        if (ebi('bbox-halp'))
            return;

        var list = [
            ['<b># hotkey</b>', '<b># operation</b>'],
            ['escape', 'close'],
            ['left, J', 'previous file'],
            ['right, L', 'next file'],
            ['home', 'first file'],
            ['end', 'last file'],
            ['R', 'rotate (shift=ccw)'],
            ['F', 'toggle fullscreen'],
            ['Z', 'toggle zoom/stretch'],
            ['S', 'toggle file selection'],
            ['G', 'cbz: go to page'],
            ['space, P, K', 'video: play / pause'],
            ['U', 'video: seek 10sec back'],
            ['O', 'video: seek 10sec ahead'],
            ['0..9', 'video: seek 0%..90%'],
            ['M', 'video: toggle mute'],
            ['V', 'video: toggle loop'],
            ['C', 'video: toggle auto-next'],
            ['<code>[</code>, <code>]</code>', 'video: loop start / end'],
        ],
            d = mknod('table', 'bbox-halp'),
            html = ['<tbody>'];

        for (var a = 0; a < list.length; a++)
            html.push('<tr><td>' + list[a][0] + '</td><td>' + list[a][1] + '</td></tr>');

        html.push('<tr><td colspan="2">tap middle of img to hide btns</td></tr>');
        html.push('<tr><td colspan="2">tap left/right sides for prev/next</td></tr>');
        d.innerHTML = html.join('\n') + '</tbody>';
        d.onclick = function () {
            overlay.removeChild(d);
        };
        overlay.appendChild(d);
    }

    function keyDownHandler(e) {
        if (modal.busy)
            return;

        if (anymod(e, true))
            return;

        var k = (e.key || e.code) + '', v = vid();

        if (k.startsWith('Key'))
            k = k.slice(3);
        else if (k.startsWith('Digit'))
            k = k.slice(5);

        var kl = k.toLowerCase();

        if (k == '?')
            return halp();

        if (k == "[" || k == "BracketLeft")
            setloop(1);
        else if (k == "]" || k == "BracketRight")
            setloop(2);
        else if (e.shiftKey && kl != "r")
            return;
        else if (k == "ArrowLeft" || k == "Left" || kl == "j")
            showLeftImage();
        else if (k == "ArrowRight" || k == "Right" || kl == "l")
            showRightImage();
        else if (k == "Escape" || k == "Esc")
            hideOverlay();
        else if (k == "Home")
            showFirstImage(e);
        else if (k == "End")
            showLastImage(e);
        else if (k == "Space" || k == "Spacebar" || kl == " " || kl == "p" || kl == "k")
            playpause();
        else if (kl == "u" || kl == "o")
            relseek(kl == "u" ? -10 : 10);
        else if (v && /^[0-9]$/.test(k) && isNum(v.duration))
            v.currentTime = v.duration * parseInt(k) * 0.1;
        else if (kl == "m" && v) {
            v.muted = vmute = !vmute;
            mp_ctl();
        }
        else if (kl == "v" && v) {
            vloop = !vloop;
            vnext = vnext && !vloop;
            setVmode();
        }
        else if (kl == "c" && v) {
            vnext = !vnext;
            vloop = vloop && !vnext;
            setVmode();
        }
        else if (kl == "f")
            tglfull();
        else if (kl == "z")
            btnZoom.click();
        else if (kl == "s")
            tglsel();
        else if (kl == "g" && isCbz)
            gotoCbzPage(e);
        else if (kl == "r")
            rotn(e.shiftKey ? -1 : 1);
        else if (kl == "y")
            dlpic();
        else
            return;
        return ev(e);
    }

    function anim() {
        var i = (anims.indexOf(options.animation) + 1) % anims.length,
            o = options;
        swrite('ganim', anims[i]);
        options = {};
        setOptions(o);
        if (tt.en)
            tt.show.call(this);
    }

    function toggleReadDir() {
        var o = options,
            next = options.readDirRtl ? "ltr" : "rtl";
        swrite('greaddir', next);
        slider.className = "no-transition";
        options = {};
        setOptions(o);
        updateOffset(true);
        window.getComputedStyle(slider).opacity; // force a restyle
        slider.className = "";

        if (tt.en)
            tt.show.call(this);
    }

    function setVmode() {
        var v = vid();
        ebi('bbox-vmode').style.display = v ? '' : 'none';
        if (!v)
            return;

        var msg = 'When video ends, ', tts = '', lbl;
        if (vloop) {
            lbl = 'Loop';
            msg += 'repeat it';
            tts = '$NHotkey: V';
        }
        else if (vnext) {
            lbl = 'Cont';
            msg += 'continue to next';
            tts = '$NHotkey: C';
        }
        else {
            lbl = 'Stop';
            msg += 'just stop'
        }
        btnVmode.setAttribute('aria-label', msg);
        btnVmode.setAttribute('tt', msg + tts);
        btnVmode.textContent = lbl;
        swrite('vmode', lbl[0]);

        v.loop = vloop
        if (vloop && v.paused)
            v.play();
    }

    function gotoCbzPage(e) {
        ev(e);

        var cur = currentIndex + 1,
            max = currentGallery.length;

        modal.prompt('Go to page (1-' + max + ')', '' + cur, function (v) {
            if (!v)
                return;

            v = parseInt(v);
            if (!isNum(v))
                return toast.warn(4, 'invalid input');

            show(Math.max(Math.min(v, max), 1) - 1);
        });
    }

    function tglVmode() {
        if (vloop) {
            vnext = true;
            vloop = false;
        }
        else if (vnext)
            vnext = false;
        else
            vloop = true;

        setVmode();
        if (tt.en)
            tt.show.call(this);
    }

    // show/label the video-source button for the current item (video + a server
    // that can transcode); the button is hidden otherwise
    function setVsrc() {
        var v = vid(),
            show = v && window.have_vcode,
            lbl = { a: 'auto', t: 'conv', n: 'orig' },
            tts = {
                a: 'video source: auto$N(native playback, transcode only if needed)',
                t: 'video source: transcode$N(re-encode; use if audio/video is broken)',
                n: 'video source: original$N(raw file; best quality, no transcode)'
            };

        btnVcode.style.display = show ? '' : 'none';
        if (!show)
            return;

        btnVcode.textContent = lbl[vsrc];
        btnVcode.setAttribute('tt', tts[vsrc]);
        btnVcode.setAttribute('aria-label', tts[vsrc].split('$N')[0]);
    }

    function tglVsrc() {
        vsrc = vsrc == 'a' ? 't' : vsrc == 't' ? 'n' : 'a';
        swrite('vsrc', vsrc);
        setVsrc();
        applyVsrc(vid());
        if (tt.en)
            tt.show.call(this);
    }

    // (re)point the current <video> at whatever source vsrc dictates, keeping
    // the playback position and play/pause state across the switch
    function applyVsrc(v) {
        if (!v || !window.have_vcode || !v.rawsrc)
            return;

        var raw = v.rawsrc,
            want = vsrc == 't' ? 't' : vsrc == 'n' ? 'n' :
                // 'auto': same decision as loadImage
                (re_vhls.test(raw) || (re_vck.test(raw) && v.canPlayType('application/vnd.apple.mpegurl'))) ? 't' : 'n';

        if (want == v.vsrc_now)
            return;  // already on the desired source

        var t = v.currentTime || 0,
            playing = !v.paused && !v.ended,
            restore = function () {
                v.removeEventListener('loadedmetadata', restore);
                try { if (t > 0.1 && isFinite(t)) v.currentTime = t; } catch (ex) { }
                if (playing) { var p = v.play(); if (p && p.catch) p.catch(function () { }); }
            };

        if (v.hls_wd) { clearTimeout(v.hls_wd); v.hls_wd = 0; }
        if (v.hls) { try { v.hls.destroy(); } catch (ex) { } v.hls = null; }
        v.addEventListener('loadedmetadata', restore);

        if (want == 't') {
            v.hls_tried = 0;
            load_hls(v, raw);  // sets vsrc_now='t'
        }
        else {
            v.hls_tried = 1;  // forced native: don't auto-fall-back to transcode
            v.vsrc_now = 'n';
            v.setAttribute('src', addq(raw, 'cache'));
            v.load();
        }
    }

    function findfile() {
        var thumb = currentGallery[currentIndex].imageElement,
            name = vsplit(thumb.href)[1].split('?')[0],
            files = msel.getall();

        for (var a = 0; a < files.length; a++)
            if (vsplit(files[a].vp)[1] == name)
                return [name, a, files, ebi(files[a].id)];
    }

    function tglfull() {
        try {
            if (isFullscreen)
                document.exitFullscreen();
            else
                ebi('bbox-overlay').requestFullscreen();
        }
        catch (ex) {
            if (IPHONE)
                alert('sorry, apple decided to make this impossible on iphones (should work on ipad tho)');
            else
                alert(ex);
        }
    }

    function setzoom() {
        var sel = clgot(btnZoom, 'on')
        clmod(ebi('bbox-overlay'), 'fill', sel);
        btnState(btnZoom, sel);
    }

    function tglsel() {
        var o = findfile()[3];
        clmod(o.closest('tr'), 'sel', 't');
        msel.selui();
        selbg();
    }

    function dlpic() {
        var url = addq(findfile()[3].href, 'cache');
        dl_file(url);
    }

    function selbg() {
        var img = vidimg(),
            thumb = currentGallery[currentIndex].imageElement,
            name = vsplit(thumb.href)[1].split('?')[0],
            files = msel.getsel(),
            sel = false;

        for (var a = 0; a < files.length; a++)
            if (vsplit(files[a].vp)[1] == name)
                sel = true;

        ebi('bbox-overlay').style.background = sel ?
            'rgba(153,34,85,0.7)' : '';

        img.style.borderRadius = sel ? '1em' : '';
        btnState(btnSel, sel);
    }

    function btnState(btn, sel) {
        btn.style.color = sel ? '#fff' : '';
        btn.style.background = sel ? '#d48' : '';
        btn.style.textShadow = sel ? '1px 1px 0 #b38' : '';
        btn.style.boxShadow = sel ? '.15em .15em 0 #502' : '';
    }

    function keyUpHandler(e) {
        if (anymod(e))
            return;

        var k = (e.key || e.code) + '';

        if (k == "Space" || k == "Spacebar" || k == " ") {
            un_pp = Date.now();
            return ev(e);
        }
    }

    var passiveSupp = false;
    try {
        var opts = {
            get passive() {
                passiveSupp = true;
                return false;
            }
        };
        window.addEventListener('test', null, opts);
        window.removeEventListener('test', null, opts);
    }
    catch (ex) {
        passiveSupp = false;
    }
    var passiveEvent = passiveSupp ? { passive: false } : null;
    var nonPassiveEvent = passiveSupp ? { passive: true } : null;

    function bindEvents() {
        bind(document, 'keydown', keyDownHandler);
        bind(document, 'keyup', keyUpHandler);
        bind(document, 'fullscreenchange', onFSC);
        bind(overlay, 'click', overlayClickHandler);
        bind(overlay, 'wheel', overlayWheelHandler);
        bind(btnPrev, 'click', showLeftImage);
        bind(btnNext, 'click', showRightImage);
        bind(btnClose, 'click', hideOverlay);
        bind(btnVmode, 'click', tglVmode);
        bind(btnVcode, 'click', tglVsrc);
        bind(btnHelp, 'click', halp);
        bind(btnAnim, 'click', anim);
        bind(btnReadDir, 'click', toggleReadDir);
        bind(btnRotL, 'click', rotl);
        bind(btnRotR, 'click', rotr);
        bind(btnSel, 'click', tglsel);
        bind(btnFull, 'click', tglfull);
        bind(btnGoPage, 'click', gotoCbzPage);
        bind(slider, 'contextmenu', contextmenuHandler);
        bind(overlay, 'touchstart', touchstartHandler, nonPassiveEvent);
        bind(overlay, 'touchmove', touchmoveHandler, passiveEvent);
        bind(overlay, 'touchend', touchendHandler);
        bind(document, 'focus', trapFocusInsideOverlay, true);
    }

    function unbindEvents() {
        unbind(document, 'keydown', keyDownHandler);
        unbind(document, 'keyup', keyUpHandler);
        unbind(document, 'fullscreenchange', onFSC);
        unbind(overlay, 'click', overlayClickHandler);
        unbind(overlay, 'wheel', overlayWheelHandler);
        unbind(btnPrev, 'click', showLeftImage);
        unbind(btnNext, 'click', showRightImage);
        unbind(btnClose, 'click', hideOverlay);
        unbind(btnVmode, 'click', tglVmode);
        unbind(btnVcode, 'click', tglVsrc);
        unbind(btnHelp, 'click', halp);
        unbind(btnAnim, 'click', anim);
        unbind(btnReadDir, 'click', toggleReadDir);
        unbind(btnRotL, 'click', rotl);
        unbind(btnRotR, 'click', rotr);
        unbind(btnSel, 'click', tglsel);
        unbind(btnFull, 'click', tglfull);
        unbind(btnGoPage, 'click', gotoCbzPage);
        unbind(slider, 'contextmenu', contextmenuHandler);
        unbind(overlay, 'touchstart', touchstartHandler, nonPassiveEvent);
        unbind(overlay, 'touchmove', touchmoveHandler, passiveEvent);
        unbind(overlay, 'touchend', touchendHandler);
        unbind(document, 'focus', trapFocusInsideOverlay, true);
        timer.rm(rotn);
    }

    function prepareOverlay(gallery, userOptions, cbz) {
        if (currentGallery === gallery)
            return;

        isCbz = cbz;
        btnGoPage.style.display = isCbz ? '' : 'none';

        currentGallery = gallery;
        setOptions(userOptions);
        slider.innerHTML = '';
        imagesElements.length = 0;

        var imagesFiguresIds = [];
        var imagesCaptionsIds = [];
        for (var i = 0, fullImage; i < gallery.length; i++) {
            fullImage = mknod('div', 'baguette-img-' + i);
            fullImage.className = 'full-image';
            imagesElements.push(fullImage);

            imagesFiguresIds.push('bbox-figure-' + i);
            imagesCaptionsIds.push('bbox-figcaption-' + i);
            slider.appendChild(imagesElements[i]);
        }
        overlay.setAttribute('aria-labelledby', imagesFiguresIds.join(' '));
        overlay.setAttribute('aria-describedby', imagesCaptionsIds.join(' '));
    }

    function setOptions(newOptions) {
        if (!newOptions)
            newOptions = {};

        for (var item in defaults) {
            options[item] = defaults[item];
            if (typeof newOptions[item] !== 'undefined')
                options[item] = newOptions[item];
        }

        var an = options.animation = sread('ganim', anims) || anims[ANIM ? 0 : 2];
        btnAnim.textContent = ['⇄', '⮺', '⚡'][anims.indexOf(an)];
        btnAnim.setAttribute('tt', 'animation: ' + an);

        options.readDirRtl = sread('greaddir') === "rtl";
        var msg;
        if (options.readDirRtl) {
            btnReadDir.innerText = "rtl";
            msg = "browse from right to left";
            slider.style.display = "flex";
            slider.style.flexDirection = "row-reverse";
        } else {
            btnReadDir.innerText = "ltr";
            msg = "browse from left to right";
            slider.style.flexDirection = "";
            slider.style.display = "block";
        }
        btnReadDir.setAttribute("tt", msg);
        btnReadDir.setAttribute("aria-label", msg);


        slider.style.transition = (options.animation === 'fadeIn' ? 'opacity .3s ease' :
            options.animation === 'slideIn' ? '' : 'none');

        if (options.buttons === 'auto' && ('ontouchstart' in window || currentGallery.length === 1))
            options.buttons = false;

        btnPrev.style.display = btnNext.style.display = (options.buttons ? '' : 'none');
    }

    function showOverlay(chosenImageIndex) {
        if (options.noScrollbars) {
            var a = document.documentElement.style.overflowY,
                b = document.body.style.overflowY;

            if (a != 'hidden' || b != 'scroll')
                scrollCSS = [a, b];

            document.documentElement.style.overflowY = 'hidden';
            document.body.style.overflowY = 'scroll';
        }
        if (overlay.style.display === 'block')
            return;

        bindEvents();
        currentIndex = chosenImageIndex;
        touch = {
            count: 0,
            startX: null,
            startY: null
        };
        loadImage(currentIndex, function () {
            preloadNext(currentIndex);
            preloadPrev(currentIndex);
        });

        show_buttons(0);

        updateOffset();
        overlay.style.display = 'block';
        // Fade in overlay
        setTimeout(function () {
            clmod(overlay, 'visible', 1);
            if (options.bodyClass && document.body.classList)
                document.body.classList.add(options.bodyClass);

            if (options.afterShow)
                options.afterShow();
        }, 50);

        if (options.onChange && !url_ts)
            options.onChange.call(currentGallery, currentIndex, imagesElements.length);

        url_ts = null;
        documentLastFocus = document.activeElement;
        btnClose.focus();
        isOverlayVisible = true;
    }

    function hideOverlay(e, dtor) {
        ev(e);
        playvid(false);
        removeFromCache('#files');
        if (options.noScrollbars) {
            document.documentElement.style.overflowY = scrollCSS[0];
            document.body.style.overflowY = scrollCSS[1];
        }

        try {
            if (document.fullscreenElement)
                document.exitFullscreen();
        }
        catch (ex) { }
        isFullscreen = false;

        if (toast.tag == 'bb-ded')
            toast.hide();

        if (dtor || overlay.style.display === 'none')
            return;

        if (options.duringHide)
            options.duringHide();

        sethash('');
        unbindEvents();

        // Fade out and hide the overlay
        clmod(overlay, 'visible');
        setTimeout(function () {
            overlay.style.display = 'none';
            if (options.bodyClass && document.body.classList)
                document.body.classList.remove(options.bodyClass);

            qsr('#bbox-halp');

            if (options.afterHide)
                options.afterHide();

            options.refocus && documentLastFocus && documentLastFocus.focus();
            isOverlayVisible = false;
            unvid();
            unfig();
        }, 250);
    }

    function unvid(keep) {
        var vids = QSA('#bbox-overlay video');
        for (var a = vids.length - 1; a >= 0; a--) {
            var v = vids[a];
            if (v == keep)
                continue;

            if (v.hls_wd) { clearTimeout(v.hls_wd); v.hls_wd = 0; }
            if (v.hls) {
                try { v.hls.destroy(); } catch (ex) { }
                v.hls = null;
            }
            unbind(v, 'touchstart', vtouch, nonPassiveEvent);
            unbind(v, 'error', lerr);
            v.src = '';
            v.load();

            var p = v.parentNode;
            p.removeChild(v);
            p.parentNode.removeChild(p);
        }
    }

    function unfig(keep) {
        var figs = QSA('#bbox-overlay figure'),
            npre = options.preload || 0,
            k = [];

        if (keep === undefined)
            keep = -9;

        for (var a = keep - npre; a <= keep + npre; a++)
            k.push('bbox-figure-' + a);

        for (var a = figs.length - 1; a >= 0; a--) {
            var f = figs[a];
            if (!has(k, f.getAttribute('id')))
                f.parentNode.removeChild(f);
        }
    }

    var _hlsjs_cbs = null;
    function ensure_hls_js(cb) {
        if (window.Hls)
            return cb();

        if (_hlsjs_cbs) {
            _hlsjs_cbs.push(cb);
            return;
        }
        _hlsjs_cbs = [cb];
        import_js(SR + '/.cpr/w/deps/hls.light.js', function () {
            var cbs = _hlsjs_cbs;
            _hlsjs_cbs = null;
            for (var a = 0; a < cbs.length; a++)
                try { cbs[a](); } catch (ex) { }
        }, function () {
            _hlsjs_cbs = null;
            toast.err(20, 'failed to load the video transcoder (hls.js)');
        });
    }

    // switch a <video> element from the (unplayable) source file to an
    // on-the-fly adaptive-bitrate HLS transcode served at  <source>/.hls/master.m3u8
    // (the master lists several renditions; the player picks by screen + bandwidth)
    function load_hls(image, raw_src) {
        if (image.hls_wd) { clearTimeout(image.hls_wd); image.hls_wd = 0; }
        image.hls_tried = 1;
        image.vsrc_now = 't';

        var u = raw_src, q = '', qi = u.indexOf('?');
        if (qi >= 0) { q = u.slice(qi); u = u.slice(0, qi); }
        var hls_url = u + '/.hls/master.m3u8' + q;

        // safari / ios play HLS natively; no library needed
        if (!window.Hls && image.canPlayType('application/vnd.apple.mpegurl')) {
            image.setAttribute('src', hls_url);
            return;
        }
        if (!window.Hls) {
            ensure_hls_js(function () { load_hls(image, raw_src); });
            return;
        }
        if (window.Hls.isSupported()) {
            if (image.hls)
                try { image.hls.destroy(); } catch (ex) { }

            // capLevelToPlayerSize keeps the auto-picked rendition no larger
            // than the video element. enableWorker=false: the default --csp-ui
            // (worker-src 'self') refuses the blob: transmuxer worker hls.js
            // would otherwise try to spawn, so this only skips the CSP error
            // in the console; the transmuxing ran on the main thread anyway
            var h = image.hls = new window.Hls({ capLevelToPlayerSize: true, enableWorker: false });
            h.on(window.Hls.Events.MANIFEST_PARSED, function () {
                var p = image.play();
                if (p && p.catch)
                    p.catch(function () { });
            });
            // a fatal error (the server refused or timed out: 404/503 on the
            // playlist or a segment) never reaches the <video> 'error' event,
            // so without this the spinner would just sit there forever
            h.on(window.Hls.Events.ERROR, function (e, d) {
                if (!d || !d.fatal)
                    return;
                var why = d.details || d.type || 'error';
                if (d.response && d.response.code)
                    why += ' (HTTP ' + d.response.code + ')';
                console.log('bb-vcode: transcode failed: ' + why);
                try { h.destroy(); } catch (ex) { }
                if (image.hls === h)
                    image.hls = null;
                if (image === vid())
                    toast.err(20, 'video transcode failed: ' + esc(why) + '\n\nswitching back to the original file', 'bb-hls');
                if (image.rawsrc && image.vsrc_now == 't')
                    native_keep_pos(image);  // hls_tried stays set: no bounce back into the transcode
            });
            h.loadSource(hls_url);
            h.attachMedia(image);
            return;
        }
        image.setAttribute('src', hls_url);
    }

    function lerr() {
        // on-the-fly transcode fallback: the browser could not decode the
        // source file, so retry through an HLS transcode (unless the HLS
        // stream itself is what failed, or this is an image)
        if (window.have_vcode && this.tagName == 'VIDEO' && this.rawsrc && !this.hls_tried) {
            console.log('bb-vcode: native playback failed; switching to transcode');
            load_hls(this, this.rawsrc);
            note_forced_conv(this);
            return;
        }

        var t;
        try {
            t = this.getAttribute('src');
            t = uricom_dec(t.split('/').pop().split('?')[0]);
        }
        catch (ex) { }

        t = 'Failed to open ' + (t?t:'file');
        console.log('bb-ded', t);
        t += '\n\nEither the file is corrupt, or your browser does not understand the file format or codec';

        try {
            t += "\n\nerr#" + this.error.code + ", " + this.error.message;
        }
        catch (ex) { }
 
        this.ded = esc(t);
        if (this === vidimg())
            toast.err(20, this.ded, 'bb-ded');
    }

    // re-point v at a new source (chosen by setsrc) while preserving the
    // playback position and play/pause state (a raw load would restart from 0)
    function reload_keep_pos(v, setsrc) {
        var t = v.currentTime || 0,
            playing = !v.paused && !v.ended,
            restore = function () {
                v.removeEventListener('loadedmetadata', restore);
                try { if (t > 0.1 && isFinite(t)) v.currentTime = t; } catch (ex) { }
                if (playing) { var p = v.play(); if (p && p.catch) p.catch(function () { }); }
            };
        v.addEventListener('loadedmetadata', restore);
        setsrc();
    }

    // switch v to the on-the-fly transcode ('conv')
    function conv_keep_pos(v) {
        reload_keep_pos(v, function () { load_hls(v, v.rawsrc); });
    }

    // switch v back to the raw file ('orig'); hls_tried stays set so we don't
    // immediately auto-bounce back to a transcode
    function native_keep_pos(v) {
        reload_keep_pos(v, function () {
            if (v.hls_wd) { clearTimeout(v.hls_wd); v.hls_wd = 0; }
            if (v.hls) { try { v.hls.destroy(); } catch (ex) { } v.hls = null; }
            v.hls_tried = 1;
            v.vsrc_now = 'n';
            v.setAttribute('src', addq(v.rawsrc, 'cache'));
            v.load();
        });
    }

    // we auto-switched a video that can't play natively over to the transcode;
    // surface it as a short, cancellable notice instead of doing it silently
    function note_forced_conv(v) {
        if (!v || v !== vid())
            return;

        toast.inf(9, 'this file can\'t play natively in your browser &mdash; switched to conv. <a href="#" id="bb-keeporig">keep original</a>', 'bb-fconv');
        var a = ebi('bb-keeporig');
        if (a)
            a.onclick = function (e) {
                ev(e);
                toast.hide();
                if (vid() === v && v.rawsrc)
                    native_keep_pos(v);
            };
    }

    // some containers (usually mkv) carry an audio codec the browser can't
    // decode (ac3/dts/eac3/truehd/pcm); the browser then plays the video fine
    // but silently, with no 'error' event -- so none of the fallbacks above
    // fire. we detect that and offer a switch to the transcode source, which
    // re-encodes the audio to something playable.

    // eligibility for the "no sound" offer (everything except the actual
    // audio-signal test, which differs per browser -- see au_check)
    function au_offer_ok(v) {
        return window.have_vcode && v && v.rawsrc &&
            vsrc == 'a' &&                            // respect manual orig/conv
            v.vsrc_now == 'n' && !v.au_warned &&
            !v.muted && v.volume > 0 &&               // not silenced by the user
            v.currentTime > 1.5 &&                    // enough played to judge
            re_vck.test(v.rawsrc);                    // mkv/ts/...: silent == broken
    }

    // lazily tap v's audio through an AnalyserNode so we can tell whether any
    // sound is actually coming out (the universal signal Firefox/Safari need,
    // since they lack webkit*DecodedByteCount). reuses copyparty's audio graph:
    // window.actx + afilt.acst is the single registry of per-element
    // MediaElementSources -- you may only ever create ONE per element, so we
    // must go through it rather than make our own. returns null (retryable)
    // until the AudioContext is actually running; routing audio through a
    // suspended context would MUTE working audio, so we never do that.
    function au_analyser(v) {
        if (v._ana)
            return v._ana;
        if (v._ana_fail)
            return null;
        try {
            if (typeof start_actx == 'function')
                start_actx();  // create + resume the shared AudioContext
            var ac = window.actx, af = window.afilt;
            if (!ac || ac.state != 'running' || !af || !af.acst)
                return null;  // not ready yet; a later timeupdate will retry

            v.id = v.id || ('bbv' + Date.now());
            var src = af.acst[v.id];
            if (!src) {
                // we own the sole source; createMediaElementSource captures the
                // element's audio, so we must wire it to the speakers ourselves
                src = af.acst[v.id] = ac.createMediaElementSource(v);
                src.connect(ac.destination);
            }
            var ana = ac.createAnalyser();
            ana.fftSize = 256;
            src.connect(ana);  // passive tap; fan-out doesn't alter the audio
            v._ana = ana;
        }
        catch (ex) {
            console.log('bb-vcode: audio probe unavailable: ' + ex);
            v._ana_fail = 1;
        }
        return v._ana || null;
    }

    // called from timeupdate/volumechange: is this video playing with no sound?
    function au_check(v) {
        if (!au_offer_ok(v))
            return;

        // chromium: exact "no audio decoder ever ran" signal -- cheap, and it
        // distinguishes broken audio from a track that's merely silent
        if (typeof v.webkitAudioDecodedByteCount == 'number') {
            if (v.webkitVideoDecodedByteCount > 0 && !v.webkitAudioDecodedByteCount)
                au_offer_conv(v);
            return;
        }

        // everyone else: measure actual output via the analyser
        var ana = au_analyser(v);
        if (!ana)
            return;

        var buf = v._anabuf || (v._anabuf = new Uint8Array(ana.frequencyBinCount));
        ana.getByteFrequencyData(buf);
        var peak = 0;
        for (var i = 0; i < buf.length; i++)
            if (buf[i] > peak) peak = buf[i];

        if (peak > 2) {
            v.au_warned = 1;  // real sound is coming out; stop checking this file
            return;
        }
        // silent right now; only conclude "dead" after a few continuous seconds
        // of playback silence (guards against quiet intros / buffering)
        if (v._sil0 == null)
            v._sil0 = v.currentTime;
        if (v.currentTime - v._sil0 >= 4)
            au_offer_conv(v);
    }

    // tiny popup offering to fix a silent video by switching to the transcode
    function au_offer_conv(v) {
        v.au_warned = 1;
        console.log('bb-vcode: no audible audio; offering transcode');
        toast.inf(15, 'no sound? your browser can\'t decode this file\'s audio codec &mdash; <a href="#" id="bb-auconv">switch to conv</a>', 'bb-auconv');
        var a = ebi('bb-auconv');
        if (a)
            a.onclick = function (e) {
                ev(e);
                toast.hide();
                if (vid() === v && v.rawsrc)
                    conv_keep_pos(v);  // transcode just this file; global mode stays 'auto'
            };
    }

    function loadImage(index, callback) {
        var imageContainer = imagesElements[index];
        var galleryItem = currentGallery[index];

        if (typeof imageContainer === 'undefined' || typeof galleryItem === 'undefined')
            return;  // out-of-bounds or gallery dirty

        if (imageContainer.querySelector('img, video'))
            // was loaded, cb and bail
            return callback ? callback() : null;

        // maybe unloaded video
        while (imageContainer.firstChild)
            imageContainer.removeChild(imageContainer.firstChild);

        var imageElement = galleryItem.imageElement,
            imageSrc = galleryItem.href || imageElement.href,
            rawSrc = imageSrc,
            is_vid = re_v.test(imageSrc),
            thumbnailElement = imageElement.querySelector('img, video'),
            imageCaption = typeof options.captions === 'function' ?
                options.captions.call(currentGallery, imageElement, index) :
                imageElement.getAttribute('data-caption') || imageElement.title;

        imageSrc = addq(imageSrc, 'cache');

        if (is_vid && index != currentIndex)
            return;  // no preload

        var figure = mknod('figure', 'bbox-figure-' + index);
        figure.innerHTML = '<div class="bbox-spinner">' +
            '<div class="bbox-double-bounce1"></div>' +
            '<div class="bbox-double-bounce2"></div>' +
            '</div>';

        if (options.captions && imageCaption) {
            var figcaption = mknod('figcaption', 'bbox-figcaption-' + index);
            figcaption.innerHTML = imageCaption;
            figure.appendChild(figcaption);
        }
        imageContainer.appendChild(figure);

        var image = mknod(is_vid ? 'video' : 'img');
        clmod(imageContainer, 'vid', is_vid);

        bind(image, 'error', lerr);
        bind(image, is_vid ? 'loadedmetadata' : 'load', function () {
            // Remove loader element
            qsr('#baguette-img-' + index + ' .bbox-spinner');
            if (!options.async && callback)
                callback();
        });
        if (is_vid)
            bind(image, 'loadedmetadata', function () {
                // the browser read the file, so cancel the never-loaded watchdog
                if (this.hls_wd) { clearTimeout(this.hls_wd); this.hls_wd = 0; }
                // container demuxed but no video track decoded -> unsupported
                // codec; chrome fails here silently (no 'error' event, so lerr
                // never fires), so kick off the transcode fallback ourselves
                if (window.have_vcode && this.rawsrc && !this.hls_tried && !this.videoWidth && re_vck.test(this.rawsrc)) {
                    console.log('bb-vcode: no decodable video track; switching to transcode');
                    load_hls(this, this.rawsrc);
                    note_forced_conv(this);
                }
            });
        if (!is_vid)
            image.setAttribute('src', imageSrc);
        else {
            // remember the un-cachebusted url so we can fall back to an HLS
            // transcode if the browser can't decode the source (see lerr)
            image.rawsrc = rawSrc;
            image.volume = clamp(fcfg_get('vol', dvol / 100), 0, 1);
            image.setAttribute('controls', 'controls');
            image.setAttribute('playsinline', '1');
            // ios ignores poster
            image.onended = vidEnd;
            image.onplay = image.onpause = ppHandler;
            // detect a video that plays but has no working audio, and offer to
            // switch to the transcode source (see au_check)
            image.ontimeupdate = function () { au_check(this); };
            image.onvolumechange = function () {
                // a user reaching for the volume is a strong "i expected sound"
                // signal, so re-check right away instead of waiting for the timer
                if (!this.muted && this.volume > 0) au_check(this);
            };
            // manual override (vsrc) wins; in 'auto' we decide per container:
            // re_vhls = no browser can demux these, always transcode; re_vck on
            // safari/ios = those play HLS natively but can't demux mkv/ts/etc and
            // won't reliably fire 'error', so transcode upfront rather than stall
            var auto_conv = vsrc == 'a' && (re_vhls.test(rawSrc) ||
                (re_vck.test(rawSrc) && image.canPlayType('application/vnd.apple.mpegurl')));
            if (window.have_vcode && (vsrc == 't' || auto_conv)) {
                load_hls(image, rawSrc);
                // auto (not a manual 'conv' choice): flag it so we can tell the
                // user we overrode native playback, once it's in the DOM (below)
                if (auto_conv)
                    image.forced_conv = 1;
            }
            else {
                image.vsrc_now = 'n';
                if (vsrc == 'n')
                    image.hls_tried = 1;  // forced native: user opted out of transcode
                image.setAttribute('src', imageSrc);
                // auto only: other browsers (chrome) content-sniff re_vck
                // containers and may neither play nor 'error', they just sit at
                // readyState 0; if metadata hasn't loaded within a few seconds,
                // fall back to a transcode (loadedmetadata cancels this)
                if (window.have_vcode && vsrc == 'a' && re_vck.test(rawSrc))
                    image.hls_wd = setTimeout(function () {
                        image.hls_wd = 0;
                        if (image.hls_tried || image.readyState >= 1)
                            return;
                        console.log('bb-vcode: native playback never started; switching to transcode');
                        load_hls(image, image.rawsrc);
                        note_forced_conv(image);
                    }, 4000);
            }
        }
        image.alt = thumbnailElement ? thumbnailElement.alt || '' : '';
        if (options.titleTag && imageCaption)
            image.title = imageCaption;

        figure.appendChild(image);

        // the upfront auto-transcode (above) ran before the <video> was in the
        // DOM; now that it's attached, surface the cancellable "keep original"
        if (image.forced_conv)
            note_forced_conv(image);

        if (is_vid && window.afilt)
            afilt.apply(undefined, image);

        if (options.async && callback)
            callback();
    }

    function ppHandler() {
        var now = Date.now();
        if (now - un_pp < 50) {
            un_pp = 0;
            return playpause();  // browser undid space hotkey
        }
        show_buttons(this.paused ? 0 : 1);
    }

    function showRightImage(e) {
        ev(e);
        var dir = options.readDirRtl ? -1 : 1;
        return show(currentIndex + dir);
    }

    function showLeftImage(e) {
        ev(e);
        var dir = options.readDirRtl ? 1 : -1;
        return show(currentIndex + dir);
    }

    function showNextImageIgnoreReadDir(e) {
        ev(e);
        return show(currentIndex + 1);
    }

    function showPreviousImageIgnoreReadDir(e) {
        ev(e);
        return show(currentIndex - 1);
    }

    function showFirstImage(e) {
        if (e)
            e.preventDefault();

        return show(0);
    }

    function showLastImage(e) {
        if (e)
            e.preventDefault();

        return show(currentGallery.length - 1);
    }

    function show(index, gallery) {
        gallery = gallery || currentGallery;
        if (!isOverlayVisible && index >= 0 && index < gallery.length) {
            prepareOverlay(gallery, options);
            showOverlay(index);
            return true;
        }

        if (index < 0)
            return bounceAnimation(options.readDirRtl ? 'right' : 'left');

        if (index >= imagesElements.length)
            return bounceAnimation(options.readDirRtl ? 'left' : 'right');

        var orot;
        try {
            orot = vidimg().getAttribute('rot');
            vid().pause();
        }
        catch (ex) { }

        currentIndex = index;
        loadImage(currentIndex, function () {
            preloadNext(currentIndex);
            preloadPrev(currentIndex);
        });
        updateOffset();

        var im = vidimg();
        if (im && im.ded)
            toast.err(20, im.ded, 'bb-ded');
        else if (toast.tag == 'bb-ded')
            toast.hide();

        if (orot && im.getAttribute('rot') === null)
            rotn(orot / 90, 1);

        if (options.animation == 'none')
            unvid(vid());
        else
            setTimeout(function () {
                unvid(vid());
            }, 100);

        unfig(index);

        if (options.onChange)
            options.onChange.call(currentGallery, currentIndex, imagesElements.length);

        return true;
    }

    var prev_cw = 0, prev_ch = 0, unrot_timer = null;
    function rotn(n, asap) {
        var el = vidimg(),
            orot = parseInt(el.getAttribute('rot') || 0),
            frot = orot + (n || 0) * 90;

        if (!frot && !orot)
            return;  // reflow noop

        var co = ebi('bbox-overlay'),
            cw = co.clientWidth,
            ch = co.clientHeight;

        if (!n && prev_cw === cw && prev_ch === ch)
            return;  // reflow noop

        clmod(el, 'asap', asap);

        prev_cw = cw;
        prev_ch = ch;
        var rot = frot,
            iw = el.naturalWidth || el.videoWidth,
            ih = el.naturalHeight || el.videoHeight,
            magic = 4,  // idk, works in enough browsers
            dl = el.closest('div').querySelector('figcaption a'),
            vw = cw,
            vh = ch - dl.offsetHeight + magic,
            pmag = Math.min(vw / ih, vh / iw),
            wmag = Math.min(vw / iw, vh / ih);

        if (!options.bbzoom) {
            pmag = Math.min(1, pmag);
            wmag = Math.min(1, wmag);
        }

        while (rot < 0) rot += 360;
        while (rot >= 360) rot -= 360;
        var q = rot == 90 || rot == 270 ? 1 : 0,
            mag = q ? pmag : wmag;

        el.style.cssText = 'max-width:none; max-height:none; position:absolute; display:block; margin:0';
        if (!orot) {
            el.style.width = iw * wmag + 'px';
            el.style.height = ih * wmag + 'px';
            el.style.left = (vw - iw * wmag) / 2 + 'px';
            el.style.top = (vh - ih * wmag) / 2 - magic + 'px';
            q = el.offsetHeight;
        }
        el.style.width = iw * mag + 'px';
        el.style.height = ih * mag + 'px';
        el.style.left = (vw - iw * mag) / 2 + 'px';
        el.style.top = (vh - ih * mag) / 2 - magic + 'px';
        el.style.transform = 'rotate(' + frot + 'deg)';
        el.setAttribute('rot', frot);
        timer.add(rotn);
        if (!rot) {
            clearTimeout(unrot_timer);
            unrot_timer = setTimeout(unrot, 300);
        }
    }
    function rotl() {
        rotn(-1);
    }
    function rotr() {
        rotn(1);
    }
    function unrot() {
        var el = vidimg(),
            orot = el.getAttribute('rot'),
            rot = parseInt(orot || 0);

        while (rot < 0) rot += 360;
        while (rot >= 360) rot -= 360;
        if (rot || orot === null)
            return;

        clmod(el, 'nt', 1);
        el.setAttribute('rot', 0);
        el.removeAttribute("style");
        rot = el.offsetHeight;
        clmod(el, 'nt');
        timer.rm(rotn);
    }

    function vid() {
        if (currentIndex >= imagesElements.length)
            return;

        return imagesElements[currentIndex].querySelector('video');
    }

    function vidimg() {
        if (currentIndex >= imagesElements.length)
            return;

        return imagesElements[currentIndex].querySelector('img, video');
    }

    function playvid(play) {
        if (!play) {
            timer.rm(loopchk);
            loopA = loopB = null;
        }

        var v = vid();
        if (!v)
            return;

        v[play ? 'play' : 'pause']();
        if (play && loopA !== null && v.currentTime < loopA)
            v.currentTime = loopA;
    }

    function playpause() {
        var v = vid();
        if (v)
            v[v.paused ? "play" : "pause"]();
    }

    function relseek(sec) {
        var v = vid(),
            t = v && v.currentTime;

        if (isNum(t))
            v.currentTime = t + sec;
    }

    function vidEnd() {
        if (this == vid() && vnext)
            showNextImageIgnoreReadDir();
    }

    function setloop(side) {
        var v = vid();
        if (!v)
            return;

        var t = v.currentTime;
        if (side == 1) loopA = t;
        if (side == 2) loopB = t;
        if (side)
            toast.inf(5, 'Loop' + (side == 1 ? 'A' : 'B') + ': ' + f2f(t, 2));

        if (loopB !== null) {
            timer.add(loopchk);
            sethash(location.hash.slice(1).split('&')[0] + '&t=' + (loopA || 0) + '-' + loopB);
        }
    }

    function loopchk() {
        if (loopB === null)
            return;

        var v = vid();
        if (!v || v.paused || v.currentTime < loopB)
            return;

        v.currentTime = loopA || 0;
    }

    function urltime(txt) {
        url_ts = txt;
    }

    function mp_ctl() {
        var v = vid();
        if (!vmute && v && mp.au && !mp.au.paused) {
            mp.fade_out();
            resume_mp = true;
        }
        else if (resume_mp && (vmute || !v) && mp.au && mp.au.paused) {
            mp.fade_in();
            resume_mp = false;
        }
    }

    function show_buttons(v) {
        clmod(ebi('bbox-btns'), 'off', v);
        clmod(btnPrev, 'off', v);
        clmod(btnNext, 'off', v);
    }

    function bounceAnimation(direction) {
        slider.className = options.animation == 'slideIn' ? 'bounce-from-' + direction : 'eog';
        setTimeout(function () {
            slider.className = '';
        }, 300);
        return false;
    }

    function updateOffset(noTransition) {
        var dir = options.readDirRtl ? 1 : -1,
            offset = dir * currentIndex * 100 + '%',
            xform = slider.style.perspective !== undefined;

        if (options.animation === 'fadeIn' && !noTransition) {
            slider.style.opacity = 0;
            setTimeout(function () {
                xform ?
                    slider.style.transform = 'translate3d(' + offset + ',0,0)' :
                    slider.style.left = offset;
                slider.style.opacity = 1;
            }, 100);
        } else {
            xform ?
                slider.style.transform = 'translate3d(' + offset + ',0,0)' :
                slider.style.left = offset;
        }
        playvid(false);
        var v = vid();
        if (v) {
            playvid(true);
            v.muted = vmute;
            v.loop = vloop;
            if (url_ts) {
                var seek = ('' + url_ts).split('-');
                v.currentTime = seek[0];
                if (seek.length > 1) {
                    loopA = parseFloat(seek[0]);
                    loopB = parseFloat(seek[1]);
                    setloop();
                }
            }
            bind(v, 'touchstart', vtouch, nonPassiveEvent);
        }
        selbg();
        mp_ctl();
        setVmode();
        setVsrc();

        var el = vidimg();
        if (el.getAttribute('rot'))
            timer.add(rotn);
        else
            timer.rm(rotn);

        var ctime = 0;
        el.onclick = v ? null : function (e) {
            var rc = e.target.getBoundingClientRect(),
                x = e.clientX - rc.left,
                fx = x / (rc.right - rc.left);

            if (fx < 0.3)
                return showLeftImage();

            if (fx > 0.7)
                return showRightImage();

            show_buttons('t');

            if (Date.now() - ctime <= 500 && !IPHONE)
                tglfull();

            ctime = Date.now();
        };

        var prev = QS('.full-image.vis');
        if (prev)
            clmod(prev, 'vis');

        clmod(el.closest('div'), 'vis', 1);
    }

    function preloadNext(index) {
        if (index - currentIndex >= options.preload)
            return;

        loadImage(index + 1, function () {
            preloadNext(index + 1);
        });
    }

    function preloadPrev(index) {
        if (currentIndex - index >= options.preload)
            return;

        loadImage(index - 1, function () {
            preloadPrev(index - 1);
        });
    }

    function bind(element, event, callback, options) {
        element.addEventListener(event, callback, options);
    }

    function unbind(element, event, callback, options) {
        element.removeEventListener(event, callback, options);
    }

    function destroyPlugin() {
        hideOverlay(undefined, true);
        unbindEvents();
        clearCachedData();
        document.getElementsByTagName('body')[0].removeChild(ebi('bbox-overlay'));
        data = {};
        currentGallery = [];
        currentIndex = 0;
    }

    return {
        run: run,
        show: show,
        showNext: showRightImage,
        showPrevious: showLeftImage,
        relseek: relseek,
        urltime: urltime,
        playpause: playpause,
        hide: hideOverlay,
        destroy: destroyPlugin
    };
})();

J_BBX = 2;
