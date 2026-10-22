import QtQuick

// The viewport's chart layer: the composed chart image.
//
// The image is published by the chart layer's own generation.  A superseded
// generation never reaches this item: the layer publishes only the
// generation that is still current.
Item {
    id: root
    property alias source: image.source
    property bool ready: image.status === Image.Ready
    property string sourceError: image.status === Image.Error ? "chart image unavailable" : ""
    // The chart layer's own state: it never changes the weather's state.
    property bool chartLoading: false
    // Preview state: when true, the shown chart is a scaled preview of the
    // last published composition, used during zoom/time changes until the
    // new composition is ready.
    property bool preview: false
    // Scale factor for the preview image (2x per zoom level difference).
    property real previewScale: 1.0
    // The composed image covers the camera's window with the application's
    // own margin, so it is placed by the offset between the composed
    // centre and the current centre.
    property real imageX: 0
    property real imageY: 0
    property real imageWidth: 0
    property real imageHeight: 0
    Image {
        id: image
        visible: true
        smooth: root.preview
        cache: false
        x: root.imageX
        y: root.imageY
        width: root.imageWidth
        height: root.imageHeight
        transform: Scale {
            origin.x: root.imageWidth / 2
            origin.y: root.imageHeight / 2
            // The preview is a scaled copy of the published composition; the
            // published composition itself is shown unscaled.  The scale is
            // always present: a Transform has no enabled state.
            xScale: root.preview ? root.previewScale : 1
            yScale: root.preview ? root.previewScale : 1
        }
    }
    // The chart layer's own loading state, in the chart layer's own scene:
    // the weather's state is never shown for the chart.
    Rectangle {
        id: status
        objectName: "chartStatus"
        visible: root.chartLoading
        x: 8
        y: 34
        width: statusText.implicitWidth + 12
        height: statusText.implicitHeight + 8
        color: "#000000be"
        Text {
            id: statusText
            anchors.centerIn: parent
            color: "white"
            font.pixelSize: 12
            text: "Chart loading…"
        }
    }
}
