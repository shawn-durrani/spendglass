- The "Locked-in wins" card and the subscriptions list now agree on when
  a recurring charge has stopped (#108). Both wait until it's overdue by
  a full interval plus a week. The wins card used to wait only twice its
  interval, so a charge that lands every six days could show as a
  cancelled win while the list still had it as active.
