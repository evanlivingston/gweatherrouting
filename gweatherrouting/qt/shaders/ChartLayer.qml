import QtQuick

// The viewport's chart layer: the composed chart image.
//
// The image is published by the chart layer's own generation.  A superseded
// generation never reaches this item: the layer publishes only the
// generation that is still current.
Item {
    id: root
    property alias source: image.source
    property alias slot0Source: image.source
    property alias slot1Source: imageBack.source
    property string slot0Token: ""
    property string slot1Token: ""
    property int frontSlot: 0
    signal slotResult(int slot, string token, bool success)
    property bool ready: (frontSlot === 0 ? image.status : imageBack.status) === Image.Ready
    property string sourceError: (frontSlot === 0 ? image.status : imageBack.status) === Image.Error ? "chart image unavailable" : ""
    // The chart layer's own state: it never changes the weather's state.
    property bool chartLoading: false
    property bool chartUnavailable: false
    // The composed image covers the camera's window with the application's
    // own margin, so it is placed by the offset between the composed
    // centre and the current centre.
    property real imageX: 0
    property real imageY: 0
    property real imageWidth: 0
    property real imageHeight: 0
    Image {
        id: image
        visible: root.frontSlot === 0
        smooth: false
        cache: false
        x: root.imageX
        y: root.imageY
        width: root.imageWidth
        height: root.imageHeight
        onStatusChanged: {
            if (root.slot0Token && (status === Image.Ready || status === Image.Error))
                root.slotResult(0, root.slot0Token, status === Image.Ready)
        }
    }
    Image {
        id: imageBack
        visible: root.frontSlot === 1
        smooth: false
        cache: false
        x: root.imageX
        y: root.imageY
        width: root.imageWidth
        height: root.imageHeight
        onStatusChanged: {
            if (root.slot1Token && (status === Image.Ready || status === Image.Error))
                root.slotResult(1, root.slot1Token, status === Image.Ready)
        }
    }
    // The chart layer's own loading state, in the chart layer's own scene:
    // the weather's state is never shown for the chart.
    Rectangle {
        id: status
        objectName: "chartStatus"
        visible: root.chartLoading || root.chartUnavailable
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
            text: root.chartUnavailable ? "Chart unavailable" : "Chart loading…"
        }
    }
}
