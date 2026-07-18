// Basic smoke test — verifies the app boots without exceptions.
import 'package:flutter_test/flutter_test.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'package:wassalny_customer/main.dart';

void main() {
  testWidgets('App boots to splash screen', (WidgetTester tester) async {
    await tester.pumpWidget(
      const ProviderScope(child: WassalnyCustomerApp()),
    );
    await tester.pump();
    expect(find.text('وصلني بنها'), findsOneWidget);
  });
}
