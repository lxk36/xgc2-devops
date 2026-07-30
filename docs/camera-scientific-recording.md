# Camera preview, scientific recording, and snapshots

## Decision

The immutable ROS bag is the scientific record. It contains the camera's native
encoded access units and every non-pixel input needed to reproduce the
Lichtblick augmentation. A live augmented view is an operator aid, not a second
recording authority.

This keeps simulation and physical experiments on the same data contract:

- simulation timestamps are in the ROS simulation clock domain;
- physical-camera timestamps retain their native clock, dequeue time, mapped
  ROS time, mapping uncertainty, and exposure/end-of-frame reference;
- a stable but initially unknown physical-camera delay is stored later in an
  immutable alignment sidecar instead of rewriting the source bag.

## Data paths

```text
Gazebo NVENC or USB native H264/MJPEG
  |
  +-- one encoded access unit --------------------------------------+
  |                                                                |
  |  ROS CompressedVideo/CompressedImage + FrameTiming + StreamInfo |
  |       +-- rosbag: canonical experiment record                  |
  |       `-- Foxglove Bridge -> Lichtblick: live augmented view   |
  |                                                                |
  `-- Gazebo H264/RTP -> Media Edge                                |
          +-- WebRTC: direct browser preview                       |
          `-- optional H264 stream-copy Matroska: viewing copy     |

Source snapshot transaction
  `-- Media Edge loopback API -> media.capture-snapshot workflow
          `-- image + optional RGB8 + manifest + hashes
```

Gazebo encodes once with NVENC and reuses the same Annex-B access unit for RTP
and ROS. The USB ROS driver passes native H264 or MJPEG bytes through; raw RGB
decode is subscriber-driven and disabled when no local algorithm needs it.

Preview and recording can run together or independently:

- a Media Edge recording remains a source consumer with zero viewers;
- viewers may join or leave without changing the recording;
- disk muxing uses a bounded non-blocking branch and cannot stall WebRTC;
- ROS bag recording is independent of Media Edge and Foxglove Bridge queues.

## ROS camera contract

The shared package `xgc_camera_msgs` defines:

- `FrameTiming`: stream/epoch/frame identity, source time validity and
  reference, native timestamp, dequeue and publish clocks, clock mapping and
  uncertainty, keyframe/discontinuity/drop information, RTP timestamp, and
  encoded size;
- `StreamInfo`: codec and bitstream format, geometry, clock domain, timestamp
  source/reference, transports, nominal frame rate, bitrate, GOP, and queue
  capacity.

The Gazebo world camera publishes:

```text
/xgc/camera/world/video_h264
/xgc/camera/world/camera_info
/xgc/camera/world/frame_timing
/xgc/camera/world/stream_info
```

The physical camera namespace `/usb_cam` publishes:

```text
/usb_cam/video                 # native H264
/usb_cam/image_raw/compressed  # native MJPEG alternative
/usb_cam/camera_info
/usb_cam/frame_timing
/usb_cam/stream_info
```

`FrameTiming.source_time` exactly matches the corresponding encoded ROS
message. Consumers join on source time and verify `(stream_id, epoch,
frame_sequence)`; they must not assume that two independent ROS publisher
queues can never drop asymmetrically.

## Scientific rosbag profile

The `camera_scientific` profile expands both configured camera roots and adds:

```text
/clock
/tf
/tf_static
/xgc/tf
/xgc/scene
/xgc/formation_scene
/gazebo/model_states
/gazebo/link_states
```

For each camera root it records `video`, `video_h264`,
`image_raw/compressed`, `camera_info`, `frame_timing`, and `stream_info`.
Topics with no current publisher consume no bag space, so the default can list
both `/xgc/camera/world` and `/usb_cam` in simulation and physical sessions.

The profile requires rosbag compression `none`: H264 and JPEG are already
compressed, and applying LZ4/BZ2 again adds CPU load with little size benefit.
The default 4K30 estimate is 24 Mbit/s:

```text
24,000,000 / 8 * 3,600 = 10.8 GB/hour
10.8 GB * 1.25 safety factor = 13.5 GB planned/hour
```

For comparison, raw RGB8 3840 x 2160 at 30 fps is approximately 746 MB/s, or
2.69 TB/hour. It is intentionally not the continuous recording format.

Before starting, the managed recorder checks:

1. `splitSizeMiB * maxSplits` can retain the planned session;
2. filesystem free space minus `minFreeSpaceGiB` is at least the planned bytes.

It writes and fsyncs `session-manifest.json` before spawning rosbag, then
updates it atomically on exit with the stop time, exit status, signal, capacity
admission, and finalized bag file sizes.

## Offline video and physical delay

The source bag remains immutable. A viewing derivative is produced without
H264 decode or re-encode:

```bash
rosrun xgc_camera_driver xgc_camera_bag_export \
  /data/session/xgc_0.bag /data/session/xgc_1.bag \
  --output /data/session/camera.mkv
```

The exporter also writes a frame timing CSV and a provenance manifest with
source bag hashes. Matroska/MP4 timing is for convenient playback; scientific
joins continue to use the bag/CSV source timestamps.

For a stable measured image delay:

```bash
rosrun xgc_camera_driver xgc_camera_alignment \
  /data/session/xgc_0.bag /data/session/xgc_1.bag \
  --image-delay-ms 120 \
  --output /data/session/alignment.yaml
```

Two `IMAGE_TIME_NS:SCENE_TIME_NS` anchors fit an affine model. Three or more
anchors create a piecewise-linear drift model. The sidecar includes source bag
hashes and never changes either bag.

## Optional Media Edge viewing recording

Enable only when a quickly playable copy is useful:

```bash
xgc-media-edge \
  ... \
  --recording-root /var/lib/xgc2/media-recordings \
  --recording-max-bitrate 36000000
```

Start and stop are loopback-only:

```bash
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -d '{"durationSeconds":3600}' \
  http://127.0.0.1:18090/api/v1/sources/gazebo_world_camera/recordings

curl --fail-with-body -X DELETE \
  http://127.0.0.1:18090/api/v1/recordings/RECORDING_ID
```

Media Edge assembles RFC 6184 access units, begins at SPS/PPS+IDR, and after a
loss, malformed packet, queue overflow, or source restart closes the valid
segment and waits for a new decoder-safe boundary. FFmpeg is used only as an
H264-to-Matroska stream-copy muxer (`-c:v copy`). Every segment and its
per-access-unit JSONL timing index are fsynced and atomically renamed.

This recording is not a replacement for the ROS bag because its RTP/ingress
timeline alone does not contain the scene, calibration, ROS clock, or physical
camera clock mapping.

## Snapshot workflow

`media.capture-snapshot` is a finite workflow/job node. It contacts only an
explicit loopback Media Edge origin and accepts:

- `sourceId`;
- `jpeg` or `png`;
- optional exact RGB8;
- an evidence label.

It performs one source-owned transaction, not a browser screenshot or periodic
JPEG poll. The committed evidence directory contains:

```text
camera-snapshots/JOB_DIGEST/
  image.jpg | image.png
  frame.rgb8                 # optional
  manifest.json
```

The manifest records camera/source/frame identity, source timestamp and its
explicit clock domain, image dimensions and format, intrinsics/distortion,
optional exact-render pose and pose frame, system capture time, file sizes, and
SHA-256 hashes. A manual trigger can connect directly to this node. A
`trigger.schedule` (`cron` or `once`), webhook, form, chat, or automation call
can drive the same node for automatic capture.

## Queue and recovery rules

- Every live queue is bounded.
- Ordinary Lichtblick telemetry is latest-only under congestion.
- H264 retains a complete GOP; after loss it rejects dependent frames until
  SPS/PPS+IDR.
- Gazebo render callbacks do no ROS serialization, filesystem I/O, or logging.
- Encoder errors, source-time rollback, or queue overflow create a new epoch
  and decoder-safe IDR boundary.
- Unknown USB timestamp clocks are rejected by default. An `assume_*` override
  is a deployment assertion, never a silent fallback to publication time.
- Capacity failure is explicit and occurs before a recorder starts.

## Release and verification order

1. publish `ros-noetic-xgc2-camera-msgs`;
2. publish camera-core and the USB camera driver;
3. publish the Gazebo camera plugin;
4. publish Media Edge;
5. deploy the platform catalog, Lichtblick, and the new seed epoch.

At minimum, verify message generation, strict C++ build, camera unit/rostests,
Media Edge unit/race/vet tests, Lichtblick queue and seek tests, platform
catalog/workflow tests, one live IDR join, a simulation reset, one bag export,
one alignment sidecar, and one snapshot evidence bundle.
