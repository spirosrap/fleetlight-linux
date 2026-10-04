import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Fleetlight in the Omarchy bar. The app writes status.json; this draws a glyph from it
// and sends show/hide, check and quit back through the app's D-Bus actions.
BarWidget {
  id: root
  moduleName: "fleetlight.status"

  property var status: ({})
  property real nowMs: Date.now()
  property bool popupOpen: false
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  // The app rewrites the file at least every check, so an old one means it is gone.
  readonly property bool running: status.running === true && nowMs / 1000 - (status.updated_at || 0) < 900
  readonly property int attention: running ? (status.attention || 0) : 0
  readonly property int updating: running ? (status.updating || 0) : 0
  readonly property string summary: running ? String(status.summary || "") : "Not running"
  readonly property string attentionText: attention + (attention === 1 ? " needs attention" : " need attention")
  readonly property string updatingText: "Updating " + updating + (updating === 1 ? " computer…" : " computers…")

  function close() { popupOpen = false }

  function read(text) {
    try {
      var parsed = JSON.parse(text)
      status = parsed && typeof parsed === "object" ? parsed : ({})
    } catch (error) {
      status = ({})
    }
  }

  function act(name) {
    close()
    if (bar) bar.run("gapplication action io.github.fleetlight.Linux " + name)
  }

  function launch() {
    close()
    if (bar) bar.run("command -v fleetlight >/dev/null && exec fleetlight; exec \"$HOME/.local/bin/fleetlight\"")
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  FileView {
    id: statusFile
    path: (Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME") + "/.local/state") + "/fleetlight/status.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.read(text())
    onLoadFailed: root.status = ({})
  }

  // The file is replaced, not rewritten, so re-read on a timer in case the watch misses it.
  Timer {
    interval: 15000
    running: true
    repeat: true
    onTriggered: {
      root.nowMs = Date.now()
      statusFile.reload()
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.updating ? "󰚰" : "󰒍"
    active: root.attention > 0 && !root.updating
    dimmed: !root.running
    tooltipText: "Fleetlight · " + (root.updating ? root.updatingText : root.summary + (root.attention ? " · " + root.attentionText : ""))
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.popupOpen = !root.popupOpen
      else if (buttonCode === Qt.MiddleButton) root.act("check")
      else if (root.running) root.act("toggle")
      else root.launch()
    }
  }

  PopupCard {
    id: popup
    anchorItem: root
    owner: root
    bar: root.bar
    open: root.popupOpen
    contentWidth: popup.fittedContentWidth(Style.space(280))
    contentHeight: popup.fittedContentHeight(column.implicitHeight)

    Column {
      id: column
      anchors.fill: parent
      spacing: Style.space(8)

      Text {
        text: "Fleetlight"
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
      }

      Text {
        textFormat: Text.PlainText
        text: root.summary
        color: root.running ? root.foreground : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
        width: parent.width
      }

      Text {
        visible: root.running
        text: root.attention ? root.attentionText : "Everything looks healthy"
        color: root.attention ? root.urgent : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      Text {
        visible: root.updating > 0
        text: root.updatingText
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
      }

      Row {
        spacing: Style.space(6)

        Button {
          text: !root.running ? "Start" : root.status.visible ? "Hide" : "Show"
          foreground: root.foreground
          bordered: true
          fontSize: Style.font.bodySmall
          onClicked: root.running ? root.act("toggle") : root.launch()
        }

        Button {
          visible: root.running
          text: "Check now"
          foreground: root.foreground
          bordered: true
          fontSize: Style.font.bodySmall
          onClicked: root.act("check")
        }

        Button {
          visible: root.running
          text: "Quit"
          foreground: root.foreground
          bordered: true
          fontSize: Style.font.bodySmall
          onClicked: root.act("quit")
        }
      }
    }
  }
}
