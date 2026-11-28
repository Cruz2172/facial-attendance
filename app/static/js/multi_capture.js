/*
 * Guided multi-photo face capture for student enrollment.
 *
 * There is NO capture button. The student follows the on-screen instruction for
 * each pose (straight, left, right, up, smile, ...). About twice a second the
 * page sends a small camera frame to the server (/enroll/pose-check), which
 * measures the head position and answers "good" or tells the student what to
 * fix. A photo is taken automatically only after the pose has been held
 * correctly for several checks in a row. The server re-checks every submitted
 * photo again when the form is posted, so the rules cannot be skipped.
 *
 * Used by templates/enroll.html and templates/student/dashboard.html.
 * It is the ONLY script that controls the enrollment camera buttons.
 *
 * Page contract (element ids):
 *   form#enrollForm  [data-capture-count] [data-hold-count] [data-check-interval]
 *   video#enrollVideo (inside a container), #videoPlaceholder, #cameraLoading
 *   #startCameraBtn, #captureBtn (hidden by this script), #stopCameraBtn, #resetBtn, #submitBtn
 *   input#face_images (hidden; receives a JSON array of JPEG data-URLs, in pose order)
 *   #captureSteps (instructions + thumbnails), #captureStatus (messages)
 */
(function () {
    'use strict';

    var POSES = [
        { title: 'Look straight at the camera', hint: 'Relaxed, neutral face. Eyes on the lens.' },
        { title: 'Turn your head to YOUR left', hint: 'Turn a little (about 15-25 degrees) and keep your whole face visible.' },
        { title: 'Turn your head to YOUR right', hint: 'Turn a little (about 15-25 degrees) and keep your whole face visible.' },
        { title: 'Raise your chin slightly (look up)', hint: 'Tilt your head up a little. Keep looking at the camera.' },
        { title: 'Smile naturally', hint: 'A clear smile, face straight to the camera.' },
        { title: 'Look straight, a little closer', hint: 'Move slightly nearer to the camera.' },
        { title: 'Lower your chin slightly (look down)', hint: 'Tilt your head down a little. Keep your eyes visible.' }
    ];

    var CHECK_SIDE = 640;          // long side of the frame sent for checking
    var STORE_SIDE = 800;          // long side of the stored photo
    var CHECK_QUALITY = 0.7;
    var JPEG_QUALITY = 0.9;
    var COOLDOWN_MS = 1400;        // pause after a photo so the next pose is a fresh start
    var MIN_BRIGHTNESS = 45;       // 0-255 average luminance
    var MAX_BRIGHTNESS = 225;
    var MAX_TOTAL_CHARS = 4200000; // stay under the server's 5 MB request limit
    var MAX_FAILURES = 6;          // consecutive failed checks before we tell the student
    var CHECK_TIMEOUT_MS = 8000;   // a stalled request must never freeze the capture loop

    function byId(id) { return document.getElementById(id); }

    function injectStyles() {
        if (byId('mc-styles')) { return; }
        var css = document.createElement('style');
        css.id = 'mc-styles';
        css.textContent =
            '.mc-banner{border-radius:10px;padding:.75rem 1rem;margin-bottom:.75rem;background:#eef4ff;border:1px solid #c9dcff}' +
            '.mc-banner.done{background:#eafaf0;border-color:#b6e8c7}' +
            '.mc-banner .mc-title{font-weight:700}' +
            '.mc-banner .mc-hint{font-size:.85rem;color:#555}' +
            '.mc-banner .mc-auto{font-size:.8rem;color:#0d6efd;margin-top:.25rem}' +
            '.mc-thumbs{display:flex;gap:.5rem;flex-wrap:wrap;justify-content:center;margin-bottom:.5rem}' +
            '.mc-thumb{width:68px;height:68px;border-radius:10px;border:2px dashed #b8c2d0;display:flex;align-items:center;' +
            'justify-content:center;font-weight:700;color:#8793a5;background:#fff;position:relative;overflow:hidden;padding:0}' +
            '.mc-thumb.current{border-color:#0d6efd;color:#0d6efd;box-shadow:0 0 0 3px rgba(13,110,253,.18)}' +
            '.mc-thumb.filled{border:2px solid #28a745;cursor:pointer}' +
            '.mc-thumb img{width:100%;height:100%;object-fit:cover;display:block}' +
            '.mc-thumb .mc-badge{position:absolute;left:3px;bottom:3px;background:rgba(0,0,0,.6);color:#fff;font-size:.7rem;' +
            'border-radius:6px;padding:0 5px;line-height:1.3}' +
            '.mc-progress{text-align:center;font-size:.85rem;color:#555}' +
            '.mc-oval{position:absolute;left:50%;top:50%;width:50%;height:82%;transform:translate(-50%,-50%);' +
            'border:3px dashed rgba(255,255,255,.9);border-radius:50%;pointer-events:none;transition:border-color .2s,box-shadow .2s;z-index:2}' +
            '.mc-oval.ok{border-style:solid;border-color:#28d058;box-shadow:0 0 14px rgba(40,208,88,.7)}' +
            '.mc-oval.bad{border-color:#ffb020}' +
            '.mc-live{position:absolute;left:50%;bottom:10px;transform:translateX(-50%);max-width:92%;padding:.35rem .8rem;' +
            'border-radius:999px;background:rgba(0,0,0,.72);color:#fff;font-size:.9rem;text-align:center;pointer-events:none;z-index:3}' +
            '.mc-live.ok{background:rgba(25,135,84,.9)}' +
            '.mc-live.bad{background:rgba(160,90,0,.9)}';
        document.head.appendChild(css);
    }

    function init() {
        var form = byId('enrollForm');
        var video = byId('enrollVideo');
        if (!form || !video) { return; }

        var startBtn = byId('startCameraBtn');
        var captureBtn = byId('captureBtn');
        var stopBtn = byId('stopCameraBtn');
        var resetBtn = byId('resetBtn');
        var submitBtn = byId('submitBtn');
        var imagesInput = byId('face_images');
        var placeholder = byId('videoPlaceholder');
        var loading = byId('cameraLoading');
        var statusBox = byId('captureStatus');
        var stepsBox = byId('captureSteps');
        if (!startBtn || !captureBtn || !stopBtn || !resetBtn || !submitBtn || !imagesInput || !stepsBox || !statusBox) {
            return;
        }

        injectStyles();

        var requested = parseInt(form.getAttribute('data-capture-count'), 10) || 5;
        var count = Math.max(1, Math.min(requested, POSES.length));
        var holdCount = Math.max(1, parseInt(form.getAttribute('data-hold-count'), 10) || 3);
        var checkInterval = Math.max(150, parseInt(form.getAttribute('data-check-interval'), 10) || 450);

        // The poses are not optional: every one of them must be captured.
        var minRequired = count;

        var photos = [];
        for (var i = 0; i < count; i++) { photos.push(null); }

        var stream = null;
        var running = false;
        var baseline = null;          // measurements from the straight photo, returned by the server
        var okStreak = 0;
        var candidate = null;
        var inFlight = false;
        var loopTimer = null;
        var cooldownUntil = 0;
        var failures = 0;
        var requestSeq = 0;           // identifies the current request so a late reply from an abandoned one is ignored
        var submitLabel = submitBtn.innerHTML;

        var checkCanvas = document.createElement('canvas');
        var holdCanvas = document.createElement('canvas');

        // The capture button no longer exists as far as the student is concerned.
        captureBtn.style.display = 'none';
        captureBtn.disabled = true;

        // Guide oval + live message, drawn over the video.
        var container = video.parentNode;
        var oval = document.createElement('div');
        oval.className = 'mc-oval';
        oval.style.display = 'none';
        var live = document.createElement('div');
        live.className = 'mc-live';
        live.setAttribute('role', 'status');
        live.setAttribute('aria-live', 'polite');
        live.style.display = 'none';
        if (container) {
            try {
                if (window.getComputedStyle && window.getComputedStyle(container).position === 'static') {
                    container.style.position = 'relative';
                }
            } catch (e) { /* ignore */ }
            container.appendChild(oval);
            container.appendChild(live);
        }

        // ---------------------------------------------------------------- helpers
        function setStatus(kind, text) {
            statusBox.textContent = '';
            if (!text) { return; }
            var box = document.createElement('div');
            box.className = 'alert alert-' + kind + ' mt-2 mb-0';
            box.setAttribute('role', kind === 'danger' ? 'alert' : 'status');
            box.textContent = text;
            statusBox.appendChild(box);
        }

        function setLive(text, state) {
            live.textContent = text || '';
            live.className = 'mc-live' + (state ? ' ' + state : '');
            oval.className = 'mc-oval' + (state === 'ok' ? ' ok' : (state === 'bad' ? ' bad' : ''));
        }

        function firstEmpty() {
            for (var k = 0; k < photos.length; k++) { if (!photos[k]) { return k; } }
            return -1;
        }

        function filledCount() {
            var n = 0;
            for (var k = 0; k < photos.length; k++) { if (photos[k]) { n++; } }
            return n;
        }

        function filledPhotos() {
            var out = [];
            for (var k = 0; k < photos.length; k++) { if (photos[k]) { out.push(photos[k]); } }
            return out;
        }

        // ---------------------------------------------------------------- rendering
        function render() {
            var next = firstEmpty();
            var filled = filledCount();

            stepsBox.textContent = '';

            var banner = document.createElement('div');
            banner.className = 'mc-banner' + (next === -1 ? ' done' : '');
            banner.setAttribute('aria-live', 'polite');
            var title = document.createElement('div');
            title.className = 'mc-title';
            var hint = document.createElement('div');
            hint.className = 'mc-hint';
            banner.appendChild(title);
            banner.appendChild(hint);
            if (next === -1) {
                title.textContent = 'All ' + count + ' photos captured';
                hint.textContent = 'Tick the consent box and press Enroll. Click a photo to retake it.';
            } else {
                title.textContent = 'Photo ' + (next + 1) + ' of ' + count + ': ' + POSES[next].title;
                hint.textContent = POSES[next].hint;
                var auto = document.createElement('div');
                auto.className = 'mc-auto';
                auto.textContent = 'Hold the pose inside the oval. The camera takes the photo by itself.';
                banner.appendChild(auto);
            }
            stepsBox.appendChild(banner);

            var thumbs = document.createElement('div');
            thumbs.className = 'mc-thumbs';
            photos.forEach(function (photo, index) {
                var cell = document.createElement('button');
                cell.type = 'button';
                if (photo) {
                    cell.className = 'mc-thumb filled';
                    cell.title = 'Click to retake photo ' + (index + 1);
                    cell.setAttribute('aria-label', 'Photo ' + (index + 1) + ' captured. Click to retake.');
                    var img = document.createElement('img');
                    img.src = photo;
                    img.alt = 'Photo ' + (index + 1);
                    cell.appendChild(img);
                    var badge = document.createElement('span');
                    badge.className = 'mc-badge';
                    badge.textContent = String(index + 1);
                    cell.appendChild(badge);
                    cell.addEventListener('click', function () { retake(index); });
                } else {
                    cell.className = 'mc-thumb' + (index === next ? ' current' : '');
                    cell.disabled = true;
                    cell.setAttribute('aria-label', 'Photo ' + (index + 1) + ' not captured yet');
                    cell.textContent = String(index + 1);
                }
                thumbs.appendChild(cell);
            });
            stepsBox.appendChild(thumbs);

            var progress = document.createElement('div');
            progress.className = 'mc-progress';
            progress.textContent = filled + ' of ' + count + ' captured (all are required)';
            stepsBox.appendChild(progress);

            imagesInput.value = filled ? JSON.stringify(filledPhotos()) : '';
            submitBtn.disabled = filled < minRequired;
            resetBtn.disabled = filled === 0;
        }

        // ---------------------------------------------------------------- camera
        function stopStream() {
            if (loopTimer) { clearTimeout(loopTimer); loopTimer = null; }
            if (stream) {
                stream.getTracks().forEach(function (track) { track.stop(); });
            }
            stream = null;
            video.srcObject = null;
            video.style.display = 'none';
            video.style.transform = '';
            if (placeholder) { placeholder.style.display = 'block'; }
            oval.style.display = 'none';
            live.style.display = 'none';
            running = false;
            inFlight = false;
            requestSeq++;
            okStreak = 0;
            candidate = null;
        }

        function cameraErrorText(error) {
            switch (error && error.name) {
                case 'NotAllowedError': return 'Please allow camera access in your browser settings.';
                case 'NotFoundError': return 'No camera found on this device.';
                case 'NotReadableError': return 'The camera is in use by another application.';
                case 'OverconstrainedError': return 'The camera could not be configured.';
                default: return (error && error.message) ? error.message : 'Unknown camera error.';
            }
        }

        startBtn.addEventListener('click', function () {
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                setStatus('danger', 'Camera is not supported in this browser. Try Chrome, Edge, Firefox or Safari over HTTPS.');
                return;
            }
            startBtn.disabled = true;
            if (loading) { loading.style.display = 'block'; }
            setStatus('info', 'Requesting camera access...');

            navigator.mediaDevices.getUserMedia({
                video: { facingMode: 'user', width: { ideal: 1280 }, height: { ideal: 720 } },
                audio: false
            }).then(function (mediaStream) {
                stream = mediaStream;
                video.srcObject = mediaStream;
                video.setAttribute('playsinline', 'true');
                video.style.display = 'block';
                video.style.transform = 'scaleX(-1)';     // mirror preview, like a mirror: turning left looks like turning left
                if (placeholder) { placeholder.style.display = 'none'; }
                if (loading) { loading.style.display = 'none'; }

                video.onloadedmetadata = function () {
                    var played = video.play();
                    var ready = function () {
                        running = true;
                        failures = 0;
                        stopBtn.disabled = false;
                        startBtn.disabled = true;
                        oval.style.display = 'block';
                        live.style.display = 'block';
                        setStatus('success', 'Camera ready. Put your face inside the oval and follow the instructions. Photos are taken automatically.');
                        setLive('Looking for your face...', '');
                        render();
                        scheduleLoop(300);
                    };
                    if (played && typeof played.then === 'function') {
                        played.then(ready).catch(function (err) {
                            setStatus('danger', 'Could not start the video: ' + cameraErrorText(err));
                            startBtn.disabled = false;
                        });
                    } else {
                        ready();
                    }
                };
            }).catch(function (error) {
                if (loading) { loading.style.display = 'none'; }
                startBtn.disabled = false;
                setStatus('danger', 'Camera error: ' + cameraErrorText(error));
            });
        });

        stopBtn.addEventListener('click', function () {
            stopStream();
            stopBtn.disabled = true;
            startBtn.disabled = false;
            setStatus('info', 'Camera stopped. Your captured photos are kept.');
            render();
        });

        // ---------------------------------------------------------------- the checking loop
        function scheduleLoop(delay) {
            if (loopTimer) { clearTimeout(loopTimer); }
            loopTimer = setTimeout(tick, delay);
        }

        function averageBrightness(sourceCanvas) {
            var probe = document.createElement('canvas');
            probe.width = 32;
            probe.height = 32;
            var pctx = probe.getContext('2d');
            pctx.drawImage(sourceCanvas, 0, 0, 32, 32);
            var data = pctx.getImageData(0, 0, 32, 32).data;
            var total = 0;
            for (var p = 0; p < data.length; p += 4) {
                total += 0.299 * data[p] + 0.587 * data[p + 1] + 0.114 * data[p + 2];
            }
            return total / (data.length / 4);
        }

        function drawFrame(target, maxSide) {
            var scale = Math.min(1, maxSide / Math.max(video.videoWidth, video.videoHeight));
            target.width = Math.round(video.videoWidth * scale);
            target.height = Math.round(video.videoHeight * scale);
            target.getContext('2d').drawImage(video, 0, 0, target.width, target.height);
        }

        function tick() {
            loopTimer = null;
            if (!running) { return; }

            if (firstEmpty() === -1) {
                setLive('All photos captured', 'ok');
                return;
            }
            if (document.hidden || inFlight || Date.now() < cooldownUntil) {
                scheduleLoop(checkInterval);
                return;
            }
            if (video.readyState < 2 || !video.videoWidth || !video.videoHeight) {
                scheduleLoop(checkInterval);
                return;
            }

            var slot = firstEmpty();

            drawFrame(holdCanvas, STORE_SIDE);                  // the frame that may become the photo
            drawFrame(checkCanvas, CHECK_SIDE);                 // the (smaller) frame that is checked

            var brightness = averageBrightness(checkCanvas);
            if (brightness < MIN_BRIGHTNESS) {
                okStreak = 0;
                setLive('Too dark: face a window or lamp.', 'bad');
                scheduleLoop(checkInterval);
                return;
            }
            if (brightness > MAX_BRIGHTNESS) {
                okStreak = 0;
                setLive('Too bright: avoid strong light on or behind you.', 'bad');
                scheduleLoop(checkInterval);
                return;
            }

            inFlight = true;
            var mine = ++requestSeq;
            var payload = { image: checkCanvas.toDataURL('image/jpeg', CHECK_QUALITY), pose_index: slot, baseline: baseline };
            var controller = (typeof AbortController !== 'undefined') ? new AbortController() : null;

            function connectionProblem() {
                inFlight = false;
                if (!running) { return; }
                failures++;
                okStreak = 0;
                setLive('Connection problem. Retrying...', 'bad');
                if (failures >= MAX_FAILURES) {
                    setStatus('danger', 'The server cannot be reached. Check your connection, then reload this page.');
                }
                scheduleLoop(Math.max(checkInterval, 1000));
            }

            // If the server never answers, give up on this request and carry on.
            var timeoutId = setTimeout(function () {
                if (mine !== requestSeq || !inFlight) { return; }
                requestSeq++;                       // anything this request returns later is ignored
                if (controller) { try { controller.abort(); } catch (e) { /* ignore */ } }
                connectionProblem();
            }, CHECK_TIMEOUT_MS);

            var options = {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
                body: JSON.stringify(payload)
            };
            if (controller) { options.signal = controller.signal; }

            fetch('/enroll/pose-check', options).then(function (response) {
                return response.json().catch(function () { return {}; }).then(function (data) {
                    return { status: response.status, data: data };
                });
            }).then(function (result) {
                clearTimeout(timeoutId);
                if (mine !== requestSeq) { return; }       // abandoned (timed out, retaken or stopped)
                inFlight = false;
                if (!running) { return; }
                handleResult(slot, result.status, result.data || {});
            }).catch(function () {
                clearTimeout(timeoutId);
                if (mine !== requestSeq) { return; }
                connectionProblem();
            });
        }

        function abandonRequest() {
            // Whatever the server answers for the request that is in flight is now stale.
            requestSeq++;
            inFlight = false;
        }

        function csrfToken() {
            var meta = document.querySelector('meta[name="csrf-token"]');
            return meta ? meta.getAttribute('content') : '';
        }

        function handleResult(slot, status, data) {
            if (slot !== firstEmpty()) {            // the photos changed while this check was running
                okStreak = 0;
                candidate = null;
                scheduleLoop(checkInterval);
                return;
            }
            if (status === 401 || data.login_required) {
                stopStream();
                setStatus('danger', 'Your login expired. Redirecting to the login page...');
                setTimeout(function () { window.location.href = '/login'; }, 2000);
                return;
            }
            if (data.csrf) {
                stopStream();
                setStatus('danger', 'This page expired. Please refresh it and start again.');
                return;
            }
            if (data.busy || status === 503 || status === 429) {
                okStreak = 0;
                setLive('Server is busy. Retrying...', '');
                scheduleLoop(Math.max(checkInterval, 1200));
                return;
            }
            if (!data.success) {
                failures++;
                okStreak = 0;
                setLive(data.message || 'Could not check this frame.', 'bad');
                if (failures >= MAX_FAILURES) {
                    setStatus('danger', 'The position check is not working. Reload the page, or contact an administrator.');
                }
                scheduleLoop(Math.max(checkInterval, 900));
                return;
            }

            failures = 0;
            if (!data.ok) {
                okStreak = 0;
                candidate = null;
                setLive(data.message || 'Adjust your position.', 'bad');
                scheduleLoop(checkInterval);
                return;
            }

            // Good pose. The frame that was just checked is still on holdCanvas.
            okStreak++;
            candidate = holdCanvas.toDataURL('image/jpeg', JPEG_QUALITY);
            if (okStreak < holdCount) {
                setLive('Good. Hold still... (' + okStreak + '/' + holdCount + ')', 'ok');
                scheduleLoop(checkInterval);
                return;
            }
            acceptPhoto(slot, data);
        }

        function acceptPhoto(slot, data) {
            if (!candidate || candidate.length < 1000) {
                okStreak = 0;
                setLive('Could not save the frame. Hold the pose again.', 'bad');
                scheduleLoop(checkInterval);
                return;
            }
            if (slot === 0) {
                if (!data.baseline) {
                    okStreak = 0;
                    setLive('Could not measure your face. Hold still and try again.', 'bad');
                    scheduleLoop(checkInterval);
                    return;
                }
                baseline = data.baseline;
            }

            photos[slot] = candidate;
            candidate = null;
            okStreak = 0;
            cooldownUntil = Date.now() + COOLDOWN_MS;

            var next = firstEmpty();
            if (next === -1) {
                setLive('All photos captured', 'ok');
                setStatus('success', 'All photos captured. Tick the consent box and press "Enroll".');
            } else {
                setLive('Photo ' + (slot + 1) + ' saved. Next: ' + POSES[next].title, 'ok');
                setStatus('success', 'Photo ' + (slot + 1) + ' saved. Next: ' + POSES[next].title + '.');
            }
            render();
            if (next !== -1) { scheduleLoop(COOLDOWN_MS); }
        }

        // ---------------------------------------------------------------- retake / reset
        function retake(index) {
            if (index === 0) {
                // Every other pose is measured against photo 1, so they must all be redone.
                for (var k = 0; k < photos.length; k++) { photos[k] = null; }
                baseline = null;
                setStatus('info', 'Photo 1 removed. The other photos are compared with it, so please retake all of them.');
            } else {
                photos[index] = null;
                setStatus('info', 'Photo ' + (index + 1) + ' removed. ' + (running
                    ? 'Retake it: ' + POSES[index].title + '.'
                    : 'Start the camera to retake it.'));
            }
            abandonRequest();
            okStreak = 0;
            candidate = null;
            render();
            if (running) { scheduleLoop(300); }
        }

        resetBtn.addEventListener('click', function () {
            for (var k = 0; k < photos.length; k++) { photos[k] = null; }
            baseline = null;
            abandonRequest();
            okStreak = 0;
            candidate = null;
            setStatus('info', 'All photos cleared. ' + (running ? 'Start again with photo 1.' : 'Start the camera to begin.'));
            render();
            if (running) { scheduleLoop(300); }
        });

        document.addEventListener('visibilitychange', function () {
            if (running && !document.hidden && !loopTimer && !inFlight) { scheduleLoop(200); }
        });

        // ---------------------------------------------------------------- submit
        function fieldValue(id) {
            var el = byId(id);
            return el ? String(el.value || '').trim() : '';
        }

        form.addEventListener('submit', function (event) {
            var required = [
                ['student_id', 'Student ID'], ['fullname', 'Full Name'], ['email', 'Email'],
                ['course', 'Course'], ['level', 'Level']
            ];
            for (var r = 0; r < required.length; r++) {
                if (byId(required[r][0]) && !fieldValue(required[r][0])) {
                    event.preventDefault();
                    setStatus('danger', 'Please fill in: ' + required[r][1] + '.');
                    return false;
                }
            }

            var filled = filledCount();
            if (filled < minRequired) {
                event.preventDefault();
                setStatus('danger', 'All ' + minRequired + ' guided photos are required (you have ' + filled + ').');
                return false;
            }
            if (imagesInput.value.length > MAX_TOTAL_CHARS) {
                event.preventDefault();
                setStatus('danger', 'The photos are too large to upload. Press Reset and capture them again.');
                return false;
            }

            submitBtn.disabled = true;
            submitBtn.textContent = '';
            var spinner = document.createElement('i');
            spinner.className = 'fas fa-spinner fa-spin';
            submitBtn.appendChild(spinner);
            submitBtn.appendChild(document.createTextNode(' Enrolling... this can take a few seconds'));
            return true;
        });

        // Coming back with the browser's Back button must not leave the button stuck.
        window.addEventListener('pageshow', function (event) {
            if (event.persisted) {
                submitBtn.innerHTML = submitLabel;
                render();
            }
        });

        window.addEventListener('beforeunload', stopStream);

        // initial state
        startBtn.disabled = false;
        stopBtn.disabled = true;
        render();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();