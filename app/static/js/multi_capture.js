/*
 * Multi-photo face capture for student enrollment.
 *
 * Guides the student through several photos with slightly different head
 * positions and expressions (straight, left, right, up, smile...). Capturing
 * varied photos lets the system recognise the student under different
 * conditions at attendance time.
 *
 * Used by templates/enroll.html and templates/student/dashboard.html.
 * It is the ONLY script that controls the enrollment camera buttons.
 *
 * Page contract (element ids):
 *   form#enrollForm  [data-capture-count] [data-min-images]
 *   video#enrollVideo, #videoPlaceholder, #cameraLoading
 *   #startCameraBtn, #captureBtn, #stopCameraBtn, #resetBtn, #submitBtn
 *   input#face_images (hidden; receives a JSON array of JPEG data-URLs)
 *   #captureSteps (instructions + thumbnails), #captureStatus (messages)
 */
(function () {
    'use strict';

    var POSES = [
        { title: 'Look straight at the camera', hint: 'Relaxed, neutral face. Eyes on the lens.' },
        { title: 'Turn your head slightly to the LEFT', hint: 'Just a little (about 15-20 degrees). Your whole face must stay visible.' },
        { title: 'Turn your head slightly to the RIGHT', hint: 'Just a little (about 15-20 degrees). Your whole face must stay visible.' },
        { title: 'Tilt your head slightly UP', hint: 'Raise your chin a little. Keep looking at the camera.' },
        { title: 'Smile naturally', hint: 'A normal smile, face straight to the camera.' },
        { title: 'Look straight, a little closer', hint: 'Move slightly nearer to the camera.' },
        { title: 'Tilt your head slightly DOWN', hint: 'Lower your chin a little. Keep your eyes visible.' }
    ];

    var MAX_SIDE = 800;            // photos are downscaled to this on the long side
    var JPEG_QUALITY = 0.9;
    var MIN_BRIGHTNESS = 45;       // 0-255 average luminance
    var MAX_BRIGHTNESS = 225;
    var MAX_TOTAL_CHARS = 4200000; // stay under the server's 5 MB request limit

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
            '.mc-thumbs{display:flex;gap:.5rem;flex-wrap:wrap;justify-content:center;margin-bottom:.5rem}' +
            '.mc-thumb{width:68px;height:68px;border-radius:10px;border:2px dashed #b8c2d0;display:flex;align-items:center;' +
            'justify-content:center;font-weight:700;color:#8793a5;background:#fff;position:relative;overflow:hidden;padding:0}' +
            '.mc-thumb.current{border-color:#0d6efd;color:#0d6efd;box-shadow:0 0 0 3px rgba(13,110,253,.18)}' +
            '.mc-thumb.filled{border:2px solid #28a745;cursor:pointer}' +
            '.mc-thumb img{width:100%;height:100%;object-fit:cover;display:block}' +
            '.mc-thumb .mc-badge{position:absolute;left:3px;bottom:3px;background:rgba(0,0,0,.6);color:#fff;font-size:.7rem;' +
            'border-radius:6px;padding:0 5px;line-height:1.3}' +
            '.mc-progress{text-align:center;font-size:.85rem;color:#555}';
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
        var minRequired = Math.max(1, Math.min(parseInt(form.getAttribute('data-min-images'), 10) || 3, count));

        var photos = [];
        for (var i = 0; i < count; i++) { photos.push(null); }

        var stream = null;
        var running = false;
        var submitLabel = submitBtn.innerHTML;

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

        function setCaptureLabel(text, iconClass) {
            captureBtn.textContent = '';
            var icon = document.createElement('i');
            icon.className = iconClass;
            captureBtn.appendChild(icon);
            captureBtn.appendChild(document.createTextNode(' ' + text));
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
            if (next === -1) {
                title.textContent = 'All ' + count + ' photos captured';
                hint.textContent = 'You can enroll now. Click a photo to retake it.';
            } else {
                title.textContent = 'Photo ' + (next + 1) + ' of ' + count + ': ' + POSES[next].title;
                hint.textContent = POSES[next].hint;
            }
            banner.appendChild(title);
            banner.appendChild(hint);
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
            progress.textContent = filled + ' of ' + count + ' captured (at least ' + minRequired + ' required)';
            stepsBox.appendChild(progress);

            // hidden field + buttons
            imagesInput.value = filled ? JSON.stringify(filledPhotos()) : '';
            submitBtn.disabled = filled < minRequired;
            resetBtn.disabled = filled === 0;
            captureBtn.disabled = !running || next === -1;
            if (next === -1) {
                setCaptureLabel('All photos captured', 'fas fa-check');
            } else {
                setCaptureLabel('Capture photo ' + (next + 1) + ' of ' + count, 'fas fa-camera');
            }
        }

        // ---------------------------------------------------------------- camera
        function stopStream() {
            if (stream) {
                stream.getTracks().forEach(function (track) { track.stop(); });
            }
            stream = null;
            video.srcObject = null;
            video.style.display = 'none';
            if (placeholder) { placeholder.style.display = 'block'; }
            running = false;
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
                video: { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 480 } },
                audio: false
            }).then(function (mediaStream) {
                stream = mediaStream;
                video.srcObject = mediaStream;
                video.setAttribute('playsinline', 'true');
                video.style.display = 'block';
                if (placeholder) { placeholder.style.display = 'none'; }
                if (loading) { loading.style.display = 'none'; }

                video.onloadedmetadata = function () {
                    var played = video.play();
                    var ready = function () {
                        running = true;
                        stopBtn.disabled = false;
                        startBtn.disabled = true;
                        setStatus('success', 'Camera ready. Follow the instructions above and press the capture button for each photo.');
                        render();
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

        // ---------------------------------------------------------------- capture
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

        captureBtn.addEventListener('click', function () {
            var slot = firstEmpty();
            if (slot === -1) { return; }
            if (!running || !stream) {
                setStatus('warning', 'The camera is not running. Press "Start Camera" first.');
                return;
            }
            if (video.readyState < 2 || !video.videoWidth || !video.videoHeight) {
                setStatus('warning', 'The camera is not ready yet. Please wait a moment and try again.');
                return;
            }

            try {
                var scale = Math.min(1, MAX_SIDE / Math.max(video.videoWidth, video.videoHeight));
                var width = Math.round(video.videoWidth * scale);
                var height = Math.round(video.videoHeight * scale);

                var canvas = document.createElement('canvas');
                canvas.width = width;
                canvas.height = height;
                canvas.getContext('2d').drawImage(video, 0, 0, width, height);

                var brightness = averageBrightness(canvas);
                if (brightness < MIN_BRIGHTNESS) {
                    setStatus('warning', 'The image is too dark. Face a light source (a window or lamp) and try again.');
                    return;
                }
                if (brightness > MAX_BRIGHTNESS) {
                    setStatus('warning', 'The image is too bright or washed out. Avoid strong light directly behind or on your face and try again.');
                    return;
                }

                var dataUrl = canvas.toDataURL('image/jpeg', JPEG_QUALITY);
                if (!dataUrl || dataUrl.length < 1000) {
                    setStatus('danger', 'Failed to capture the image. Please try again.');
                    return;
                }

                photos[slot] = dataUrl;
                var next = firstEmpty();
                if (next === -1) {
                    setStatus('success', 'All photos captured. Tick the consent box and press "Enroll".');
                } else {
                    setStatus('success', 'Photo ' + (slot + 1) + ' saved. Next: ' + POSES[next].title + '.');
                }
                render();
            } catch (error) {
                setStatus('danger', 'Error capturing the image: ' + (error && error.message ? error.message : 'unknown error'));
            }
        });

        // ---------------------------------------------------------------- retake / reset
        function retake(index) {
            photos[index] = null;
            setStatus('info', 'Photo ' + (index + 1) + ' removed. ' + (running
                ? 'Retake it with: ' + POSES[index].title + '.'
                : 'Start the camera to retake it.'));
            render();
        }

        resetBtn.addEventListener('click', function () {
            for (var k = 0; k < photos.length; k++) { photos[k] = null; }
            setStatus('info', 'All photos cleared. ' + (running ? 'Start again with photo 1.' : 'Start the camera to begin.'));
            render();
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
                setStatus('danger', 'Please capture at least ' + minRequired + ' photos (you have ' + filled + ').');
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