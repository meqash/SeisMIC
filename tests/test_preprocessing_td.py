'''
:copyright:
:license:
   `GNU Lesser General Public License, Version 3 <https://www.gnu.org/copyleft/lesser.html>`
:author:
   Peter Makus (makus@gfz-potsdam.de)

Created: Tuesday, 20th July 2021 03:54:28 pm
Last Modified: Tuesday, 20th July 2021 04:04:29 pm
'''
import unittest

import numpy as np
from scipy.fftpack import next_fast_len
from scipy.signal.windows import gaussian

from miic3.correlate import preprocessing_td as pptd


class TestClip(unittest.TestCase):
    def test_result(self):
        args = {}
        args['std_factor'] = np.random.randint(2, 4)
        npts = np.random.randint(400, 749)
        A = np.tile(gaussian(npts, 180), (2, 1)).T
        res = pptd.clip(A.copy(), args, {})
        self.assertAlmostEqual(
            np.std(A, axis=0)[0]*args['std_factor'], abs(res).max(axis=0)[0])

    def test_std_0(self):
        args = {}
        args['std_factor'] = np.random.randint(2, 4)
        A = np.ones((100, 5))
        res = pptd.clip(A.copy(), args, {})
        self.assertTrue(np.all(res == np.zeros_like(A)))


class TestMute(unittest.TestCase):
    def setUp(self):
        self.params = {}
        self.params['sampling_rate'] = 25

    def test_return_zeros(self):
        # function is supposed to return zeros if input shorter than
        # the taper length
        npts = np.random.randint(100, 599)
        A = np.ones((npts, np.random.randint(2, 55)))
        args = {}
        args['taper_len'] = self.params['sampling_rate']*(
            npts + np.random.randint(1, 99))
        self.assertTrue(
            np.all(pptd.mute(A, args, self.params) == np.zeros_like(A)))

    def test_taper_len_error(self):
        args = {}
        args['taper_len'] = 0
        A = np.ones((5, 2))
        with self.assertRaises(ValueError):
            pptd.mute(A, args, self.params)

    def test_mute_std(self):
        # testing the actual muting of the bit
        args = {}
        args['taper_len'] = 1
        args['extend_gaps'] = True
        npts = np.random.randint(400, 749)
        A = np.tile(gaussian(npts, 180), (2, 1)).T
        res = pptd.mute(A.copy(), args, self.params)
        self.assertLessEqual(
            res[:, 0].max(axis=0), np.std(A))

    def test_mute_std_factor(self):
        # testing the actual muting of the bit
        args = {}
        args['taper_len'] = 1
        args['extend_gaps'] = True
        args['std_factor'] = np.random.randint(1, 5)
        npts = np.random.randint(400, 749)
        A = np.tile(gaussian(npts, 180), (2, 1)).T
        res = pptd.mute(A.copy(), args, self.params)
        self.assertLessEqual(
            res[:, 0].max(axis=0),
            args['std_factor']*np.std(A))

    def test_mute_absolute(self):
        args = {}
        args['taper_len'] = 1
        args['extend_gaps'] = True
        npts = np.random.randint(400, 749)
        A = np.tile(gaussian(npts, 180), (2, 1)).T
        args['threshold'] = A[:, 0].max(axis=0)/np.random.randint(2, 4)
        res = pptd.mute(A.copy(), args, self.params)
        self.assertLessEqual(
            res[:, 0].max(axis=0), args['threshold'])


class TestNormalizeStd(unittest.TestCase):
    def test_result(self):
        npts = np.random.randint(400, 749)
        A = np.tile(gaussian(npts, 180), (2, 1)).T
        res = pptd.normalizeStandardDeviation(A, {}, {})
        self.assertAlmostEqual(np.std(res, axis=0)[0], 1)

    def test_std_0(self):
        # Feed in DC signal to check this
        A = np.ones((250, 2))
        res = pptd.normalizeStandardDeviation(A.copy(), {}, {})
        self.assertTrue(np.all(res == A))


class TestFDSignBitNormalisation(unittest.TestCase):
    # Not much to test here
    def test_result(self):
        np.random.seed(2)
        dim = (np.random.randint(200, 766), np.random.randint(2, 44))
        A = np.random.random(dim)-.5
        expected_result = np.sign(A)
        self.assertTrue(np.allclose(
            expected_result, pptd.signBitNormalization(A, {}, {})))


class TestSpectralWhitening(unittest.TestCase):
    def setUp(self):
        dim = (np.random.randint(200, 766), np.random.randint(2, 44))
        self.A = np.random.random(dim) + np.random.random(dim) * 1j

    def test_result(self):
        # Again so straightforward that I wonder whether it makes sense
        # to test this
        expected = self.A/abs(self.A)
        expected[0, :] = 0.j
        self.assertTrue(np.allclose(
            expected, pptd.spectralWhitening(self.A, {}, {})))

    def test_joint_norm_not_possible(self):
        with self.assertRaises(AssertionError):
            pptd.spectralWhitening(
                np.ones((5, 5)), {'joint_norm': True}, {})

    def test_empty_array(self):
        A = np.array([])
        with self.assertRaises(IndexError):
            pptd.spectralWhitening(
                A, {}, {})


class TestTDNormalisation(unittest.TestCase):
    def setUp(self):
        self.params = {}
        self.params['sampling_rate'] = 25

    def test_win_length_error(self):
        args = {}
        args['windowLength'] = 0
        with self.assertRaises(ValueError):
            pptd.TDnormalization(np.ones((5, 2)), args, self.params)

    # def test_result(self):
    # Gotta think a little about that one
    #     args = {}
    #     args['windowLength'] = 4
    #     args['filter'] = False
    #     A = np.ones(
    #         (np.random.randint(600, 920),
    #             np.random.randint(2, 8)))*np.random.randint(2, 8)
    #     res = pptd.TDnormalization(A.copy(), args, self.params)
    #     self.assertLessEqual(res.max(), 1)


class TestZeroPadding(unittest.TestCase):
    def setUp(self):
        self.params = {'sampling_rate': 25, 'lengthToSave': 200}
        self.A = np.empty(
            (np.random.randint(100, 666), np.random.randint(2, 45)))

    def test_result_next_fast_len(self):
        expected_len = next_fast_len(self.A.shape[0])
        self.assertEqual(pptd.zeroPadding(
            self.A, {'type': 'nextFastLen'}, self.params).shape[0],
            expected_len)

    def test_result_avoid_wrap_around(self):
        expected_len = self.A.shape[0] + \
            self.params['sampling_rate'] * self.params['lengthToSave']
        self.assertEqual(pptd.zeroPadding(
            self.A, {'type': 'avoidWrapAround'}, self.params).shape[0],
            expected_len)

    def test_result_avoid_wrap_fast_len(self):
        expected_len = next_fast_len(int(
            self.A.shape[0] +
            self.params['sampling_rate'] * self.params['lengthToSave']))
        self.assertEqual(pptd.zeroPadding(
            self.A, {'type': 'avoidWrapFastLen'}, self.params).shape[0],
            expected_len)

    def test_result_next_fast_len_axis1(self):
        expected_len = next_fast_len(self.A.shape[1])
        self.assertEqual(pptd.zeroPadding(
            self.A, {'type': 'nextFastLen'}, self.params, axis=1).shape[1],
            expected_len)

    def test_result_avoid_wrap_around_axis1(self):
        expected_len = self.A.shape[1] + \
            self.params['sampling_rate'] * self.params['lengthToSave']
        self.assertEqual(pptd.zeroPadding(
            self.A, {'type': 'avoidWrapAround'}, self.params, axis=1).shape[1],
            expected_len)

    def test_result_avoid_wrap_fast_len_axis1(self):
        expected_len = next_fast_len(int(
            self.A.shape[1] +
            self.params['sampling_rate'] * self.params['lengthToSave']))
        self.assertEqual(pptd.zeroPadding(
            self.A,
            {'type': 'avoidWrapFastLen'}, self.params, axis=1).shape[1],
            expected_len)

    def test_weird_axis(self):
        with self.assertRaises(NotImplementedError):
            pptd.zeroPadding(self.A, {}, {}, axis=7)

    def test_higher_dim(self):
        with self.assertRaises(NotImplementedError):
            pptd.zeroPadding(np.ones((3, 3, 3)), {}, {})

    def test_unknown_method(self):
        with self.assertRaises(ValueError):
            pptd.zeroPadding(self.A, {'type': 'blub'}, self.params)

    def test_empty_array(self):
        B = np.array([])
        with self.assertRaises(ValueError):
            pptd.zeroPadding(B, {'type': 'nextFastLen'}, self.params)


if __name__ == "__main__":
    unittest.main()