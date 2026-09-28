import UIKit
import XCTest

@testable import Omnigent

final class KeyboardViewportTests: XCTestCase {
  private let landscape = CGRect(x: 0, y: 0, width: 1210, height: 834)

  func testCompactHardwareToolbarKeepsFullHeight() {
    // Actual iPad hardware-toolbar frame with followsUndockedKeyboard enabled.
    let toolbar = CGRect(x: 496.5, y: 764.5, width: 217, height: 49.5)
    XCTAssertEqual(keyboardViewportHeight(in: landscape, keyboardFrame: toolbar), 834)
  }

  func testFloatingKeyboardKeepsFullHeightEvenAtBottomEdge() {
    let keyboard = CGRect(x: 880, y: 574, width: 320, height: 260)
    XCTAssertEqual(keyboardViewportHeight(in: landscape, keyboardFrame: keyboard), 834)
  }

  func testUndockedFullWidthKeyboardKeepsFullHeight() {
    let keyboard = CGRect(x: 0, y: 400, width: 1210, height: 300)
    XCTAssertEqual(keyboardViewportHeight(in: landscape, keyboardFrame: keyboard), 834)
  }

  func testDockedSoftwareKeyboardReservesSpaceInBothOrientations() {
    let keyboard = CGRect(x: 0, y: 480, width: 1210, height: 354)
    XCTAssertEqual(keyboardViewportHeight(in: landscape, keyboardFrame: keyboard), 480)
    let portrait = CGRect(x: 0, y: 0, width: 834, height: 1210)
    let portraitKeyboard = CGRect(x: 0, y: 850, width: 834, height: 360)
    XCTAssertEqual(keyboardViewportHeight(in: portrait, keyboardFrame: portraitKeyboard), 850)
  }

  func testDismissedKeyboardKeepsFullHeight() {
    let hidden = CGRect(x: 0, y: 834, width: 1210, height: 0)
    XCTAssertEqual(keyboardViewportHeight(in: landscape, keyboardFrame: hidden), 834)
    XCTAssertEqual(keyboardViewportHeight(in: landscape, keyboardFrame: .zero), 834)
  }
}
