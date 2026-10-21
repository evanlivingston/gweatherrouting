import QtQuick

// The viewport's scene with the chart layer.
//
// The chart layer is the application's chart composition, published as one
// image.  It has its own state and its own generation: the weather layers
// are published before the chart is ready, and adding the chart resets
// neither the weather nor the camera nor the forecast time.
//
// The ENC cells are part of the same composition: they are painted into the
// same image, in the application's own order.  Their state is reported
// beside the chart's, never in place of it: a coverage miss says so
// instead of showing an empty chart that reads as "there is no data here".
//
// P2.2.1: ChartGeometry item renders retained QSG geometry for ENC area fills.
// It is shown when ready; the chart Image shows the preview during zoom.
//
// The scene is the viewport's own: the application's default path never
// loads it.
Item {
    id: root
    property bool fillEnabled: false
    // The ENC source's own state, in the scene's own words.  An empty
    // string means there is nothing to report.
    property string encStatus: ""
    // P2.2.1: Container for retained QSG geometry (area fills).
    // Geometry items are created in Python and parented here.
    Item {
        id: chartGeometry
        objectName: "chartGeometry"
        anchors.fill: parent
        z: -1  // Behind the chart image
    }
    ChartLayer {
        id: chart
        objectName: "chartLayer"
        anchors.fill: parent
    }
    // Application-owned MBTiles/weather background/marine stay above ENC,
    // below wind barbs. Standalone use leaves this image empty.
    ChartLayer {
        objectName: "environmentLayer"
        anchors.fill: parent
    }
    WeatherFill {
        id: fill
        objectName: "weatherFill"
        anchors.fill: parent
        visible: root.fillEnabled
    }
    WeatherBarbs {
        id: barbs
        objectName: "weatherBarbs"
        anchors.fill: parent
    }
    // The ENC source's own status.  It is drawn last, so it stays readable
    // over the weather layers, and it never shows the weather's state.
    Rectangle {
        id: encStatus
        objectName: "encStatus"
        visible: root.encStatus !== ""
        x: 8
        y: 62
        width: encStatusText.implicitWidth + 12
        height: encStatusText.implicitHeight + 8
        color: "#000000be"
        Text {
            id: encStatusText
            anchors.centerIn: parent
            color: "white"
            font.pixelSize: 12
            text: root.encStatus
        }
    }
}
